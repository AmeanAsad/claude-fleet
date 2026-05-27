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
import shutil
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
        # name -> {"proc": Popen|None, "pid": int}
        # "proc" is set when we spawned the worker ourselves; "pid" is always set.
        # Adopted workers (registered with the server but not spawned by us) have proc=None.
        self.workers: dict[str, dict] = {}
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
        # Best-effort SSH info so the server can wire up `cfleet attach` for
        # this machine. Empty values are ignored by the server.
        ssh_user = os.environ.get("USER", "")
        ssh_host = os.environ.get("CFLEET_SSH_HOST", "")
        return {
            "hostname": platform.node(),
            "os": f"{platform.system()} {platform.release()}",
            "arch": platform.machine(),
            "python": platform.python_version(),
            "cpus": os.cpu_count() or 1,
            "ssh_user": ssh_user,
            "ssh_host": ssh_host,
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
                # Adopt any pre-existing workers before announcing ourselves so
                # the server's first view matches reality on this host.
                self._reconcile_workers()
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
                # Refresh worker map: adopt newly-registered workers from the server,
                # and drop any dead PIDs.
                self._reconcile_workers()
                alive = list(self.workers.keys())
                await ws.send(json.dumps({
                    "type": "heartbeat",
                    "worker_names": alive,
                }))
            except Exception:
                break

    def _is_alive(self, entry: dict) -> bool:
        """Check whether a worker entry's process is still running."""
        proc = entry.get("proc")
        if proc is not None:
            return proc.poll() is None
        pid = entry.get("pid")
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _find_pid_by_worker_name(self, worker_name: str) -> int | None:
        """Find a running `cfleet agent <worker_name>` process. Returns PID or None."""
        import re as _re
        if not _re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", worker_name):
            return None
        try:
            p = subprocess.run(
                ["pgrep", "-f", f"cfleet agent {worker_name}"],
                capture_output=True, text=True,
            )
            if p.returncode != 0 or not p.stdout.strip():
                return None
            # Take the first match; multiple shouldn't happen but pgrep is line-per-pid.
            return int(p.stdout.strip().splitlines()[0])
        except (FileNotFoundError, ValueError):
            return None

    def _server_worker_names(self) -> list[str]:
        """Ask the server which workers are registered for this machine."""
        import urllib.request, urllib.error
        try:
            req = urllib.request.Request(
                f"{self.server_url}/api/machines/{self.machine_name}/workers",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            if isinstance(data, list):
                return [str(n) for n in data]
        except Exception:
            pass
        return []

    def _reconcile_workers(self) -> None:
        """Sync self.workers with the server's view + actual processes on this host.

        - Adopt server-known workers whose process is running but we don't track.
        - Drop entries whose PID is dead.
        """
        # Drop dead entries
        for name in list(self.workers.keys()):
            if not self._is_alive(self.workers[name]):
                del self.workers[name]

        # Adopt new ones
        for name in self._server_worker_names():
            if name in self.workers:
                continue
            pid = self._find_pid_by_worker_name(name)
            if pid:
                self.workers[name] = {"proc": None, "pid": pid}

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
        raw_cwd = data.get("cwd", "")
        cwd = os.path.abspath(os.path.expanduser(raw_cwd)) if raw_cwd else ""
        skip_permissions = bool(data.get("skip_permissions", True))

        if worker_name in self.workers:
            await ws.send(json.dumps({
                "type": "response",
                "request_id": request_id,
                "data": {"error": f"Worker {worker_name} already running on this machine"},
            }))
            return

        try:
            from cfleet.config import FleetConfig

            try:
                config = FleetConfig.load()
            except FileNotFoundError:
                config = FleetConfig()

            # Cwd default + scaffolding (inbox/outbox/repos/CLAUDE.md) live in
            # `cfleet agent` itself so manual launches behave the same.
            worker_dir = cwd or os.path.join(os.path.expanduser("~"), worker_name)
            os.makedirs(worker_dir, exist_ok=True)

            if repos:
                # Honor --repo: clone into worker_dir/repos/ before agent boot.
                for r in repos:
                    dest = Path(worker_dir) / "repos" / r["name"]
                    if dest.exists():
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    branch = r.get("branch", "main")
                    subprocess.run(
                        ["git", "clone", "--depth", "1", "--single-branch",
                         "-b", branch, r["url"], str(dest)],
                        check=False,
                    )

            env = os.environ.copy()
            env["ANTHROPIC_API_KEY"] = self.api_key or config.anthropic_api_key
            env["CLAUDE_CODE_API_KEY"] = env["ANTHROPIC_API_KEY"]

            # Resolve the cfleet binary. machine_agent itself runs from a cfleet
            # install, so cfleet is on PATH (or alongside this interpreter).
            cfleet_bin = shutil.which("cfleet") or "cfleet"
            cmd = [
                cfleet_bin, "agent", worker_name,
                "--cwd", worker_dir,
                "--model", model,
                "--server-url", self.server_url,
                "--token", self.token,
                "--skip-permissions" if skip_permissions else "--no-skip-permissions",
            ]

            proc = subprocess.Popen(cmd, env=env)
            self.workers[worker_name] = {"proc": proc, "pid": proc.pid}

            await ws.send(json.dumps({
                "type": "response",
                "request_id": request_id,
                "data": {"ok": True, "worker_name": worker_name},
            }))

        except Exception as e:
            await ws.send(json.dumps({
                "type": "response",
                "request_id": request_id,
                "data": {"error": str(e)},
            }))

    async def _handle_kill(self, ws, data: dict, request_id: str) -> None:
        worker_name = data.get("worker_name", "")
        purge_session = bool(data.get("purge_session", False))
        session_id = data.get("session_id", "")
        cwd = data.get("cwd", "")

        # Make sure we know about all workers on this machine (including ones
        # spawned manually via `cfleet agent` outside our spawn path).
        self._reconcile_workers()
        entry = self.workers.pop(worker_name, None)

        if entry is None:
            await ws.send(json.dumps({
                "type": "response",
                "request_id": request_id,
                "data": {"error": f"Worker {worker_name} not found on this machine"},
            }))
            return

        proc = entry.get("proc")
        pid = entry.get("pid")
        killed_via = "owned" if proc else "adopted"
        try:
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            elif pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                # Wait up to ~5s for graceful exit
                for _ in range(20):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.25)
                else:
                    try:
                        os.kill(pid, signal.SIGKILL)
                        killed_via = "adopted-kill"
                    except ProcessLookupError:
                        pass
        except Exception:
            pass

        purged = False
        if purge_session and session_id and cwd:
            try:
                from pathlib import Path
                encoded_cwd = cwd.replace("/", "-")
                proj_dir = Path.home() / ".claude" / "projects" / encoded_cwd
                for suffix in (".jsonl", ".lock"):
                    p = proj_dir / f"{session_id}{suffix}"
                    if p.exists():
                        p.unlink()
                        purged = True
                # Also remove the session-id sidecar dir (claude --resume metadata)
                sidecar = proj_dir / session_id
                if sidecar.exists() and sidecar.is_dir():
                    import shutil as _shutil
                    _shutil.rmtree(sidecar, ignore_errors=True)
            except Exception as e:
                print(f"[machine] purge_session failed for {worker_name}: {e}")

        await ws.send(json.dumps({
            "type": "response",
            "request_id": request_id,
            "data": {
                "ok": True,
                "worker_name": worker_name,
                "session_purged": purged,
                "killed_via": killed_via,
            },
        }))

    def _handle_signal(self) -> None:
        self._shutdown = True

    async def _cleanup(self) -> None:
        print("[machine] Shutting down, stopping workers we own (adopted ones are left running)...")
        for name, entry in self.workers.items():
            proc = entry.get("proc")
            if proc is None:
                continue
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
