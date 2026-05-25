"""Machine agent — daemon that connects to the fleet server and manages local workers.

Started by `cfleet join`. Maintains a persistent WebSocket to the server's
/ws/machine endpoint, receives spawn/kill commands, and manages worker relay
subprocesses.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


class MachineAgent:
    """Machine-side daemon that registers with the fleet server and handles commands."""

    def __init__(
        self,
        server_url: str,
        token: str,
        machine_name: str,
        api_key: str = "",
        model: str = "claude-opus-4-6",
    ):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.machine_name = machine_name
        self.api_key = api_key
        self.model = model
        self.workers: dict[str, subprocess.Popen] = {}
        self._next_port = 8421
        self._shutdown = False

    def _ws_url(self) -> str:
        return (
            self.server_url
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            + "/ws/machine"
        )

    def _system_info(self) -> dict:
        return {
            "hostname": platform.node(),
            "os": f"{platform.system()} {platform.release()}",
            "arch": platform.machine(),
            "python": platform.python_version(),
            "cpus": os.cpu_count() or 1,
        }

    async def run(self) -> None:
        """Connect to server and enter the command loop. Reconnects on failure."""
        import websockets

        backoff = 1.0
        max_backoff = 60.0

        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT, self._handle_signal)
        loop.add_signal_handler(signal.SIGTERM, self._handle_signal)

        while not self._shutdown:
            try:
                async with websockets.connect(self._ws_url()) as ws:
                    await ws.send(json.dumps({
                        "type": "register",
                        "token": self.token,
                        "machine_name": self.machine_name,
                        "system_info": self._system_info(),
                        "worker_names": list(self.workers.keys()),
                    }))

                    reg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                    if reg.get("type") != "registered":
                        print(f"[machine] Registration failed: {reg}")
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, max_backoff)
                        continue

                    print(f"[machine] Connected as {self.machine_name}")
                    backoff = 1.0

                    heartbeat_task = asyncio.create_task(self._heartbeat(ws))

                    try:
                        async for raw in ws:
                            if self._shutdown:
                                break
                            data = json.loads(raw)
                            await self._handle_message(ws, data)
                    finally:
                        heartbeat_task.cancel()

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._shutdown:
                    break
                print(f"[machine] Connection error: {e}, reconnecting in {backoff:.0f}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

        await self._cleanup()

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                alive = []
                for name, proc in list(self.workers.items()):
                    if proc.poll() is None:
                        alive.append(name)
                    else:
                        del self.workers[name]

                await ws.send(json.dumps({
                    "type": "heartbeat",
                    "worker_names": alive,
                }))
            except Exception:
                break

    async def _handle_message(self, ws, data: dict) -> None:
        msg_type = data.get("type")
        request_id = data.get("request_id", "")

        if msg_type == "heartbeat_ack":
            return

        if msg_type == "spawn_worker":
            await self._handle_spawn(ws, data, request_id)
        elif msg_type == "kill_worker":
            await self._handle_kill(ws, data, request_id)
        elif msg_type == "ping":
            await ws.send(json.dumps({"type": "pong", "request_id": request_id}))

    async def _handle_spawn(self, ws, data: dict, request_id: str) -> None:
        worker_name = data.get("worker_name", "")
        model = data.get("model", self.model)
        repos = data.get("repos", [])
        cwd = data.get("cwd", "")

        if worker_name in self.workers:
            await ws.send(json.dumps({
                "type": "response",
                "request_id": request_id,
                "data": {"error": f"Worker {worker_name} already running on this machine"},
            }))
            return

        try:
            from cfleet.provisioner import local_provision_worker
            from cfleet.config import FleetConfig

            try:
                config = FleetConfig.load()
            except FileNotFoundError:
                config = FleetConfig()

            workspace = cwd if cwd else None
            if not workspace:
                worker_dir = local_provision_worker(
                    worker_name=worker_name,
                    relay_port=self._next_port,
                    model=model,
                    repos=repos,
                    fleet_config=config,
                )
            else:
                worker_dir = cwd

            relay_script = self._find_relay_script()
            port = self._next_port
            self._next_port += 1

            env = os.environ.copy()
            env["ANTHROPIC_API_KEY"] = self.api_key or config.anthropic_api_key
            env["CLAUDE_CODE_API_KEY"] = env["ANTHROPIC_API_KEY"]
            env["CFLEET_MODEL"] = model

            cmd = [
                sys.executable, relay_script,
                "--port", str(port),
                "--host", "127.0.0.1",
                "--model", model,
                "--cwd", worker_dir,
                "--server-url", self.server_url,
                "--token", self.token,
                "--worker-name", worker_name,
                "--machine-name", self.machine_name,
            ]

            proc = subprocess.Popen(cmd, env=env)
            self.workers[worker_name] = proc

            await ws.send(json.dumps({
                "type": "response",
                "request_id": request_id,
                "data": {"ok": True, "worker_name": worker_name, "relay_port": port},
            }))

        except Exception as e:
            await ws.send(json.dumps({
                "type": "response",
                "request_id": request_id,
                "data": {"error": str(e)},
            }))

    async def _handle_kill(self, ws, data: dict, request_id: str) -> None:
        worker_name = data.get("worker_name", "")
        proc = self.workers.pop(worker_name, None)

        if proc is None:
            await ws.send(json.dumps({
                "type": "response",
                "request_id": request_id,
                "data": {"error": f"Worker {worker_name} not found on this machine"},
            }))
            return

        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass

        await ws.send(json.dumps({
            "type": "response",
            "request_id": request_id,
            "data": {"ok": True, "worker_name": worker_name},
        }))

    def _find_relay_script(self) -> str:
        candidates = [
            Path("/opt/cfleet-relay/worker_relay.py"),
            Path.home() / ".cfleet" / "relay" / "worker_relay.py",
            Path(__file__).parent / "worker_relay.py",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        raise FileNotFoundError("worker_relay.py not found")

    def _handle_signal(self) -> None:
        self._shutdown = True

    async def _cleanup(self) -> None:
        print("[machine] Shutting down, stopping workers...")
        for name, proc in self.workers.items():
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.workers.clear()
        print("[machine] Disconnected.")
