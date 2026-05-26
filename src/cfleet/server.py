"""Central fleet server — WebSocket hub for worker registration + REST API for CLI.

Workers connect outbound via WebSocket, register with a token, and receive
commands (ask, interrupt). Events stream back over WebSocket to the server,
which exposes them to CLI/TUI/web via SSE.

Start with: cfleet serve
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from cfleet.config import FleetConfig, FleetState, GitHubLevel, VMType
from cfleet.engine import FleetEngine

def _resolve_static_dir() -> Path:
    """Locate the Next.js static export.

    Installed wheels ship it as cfleet/web_out/. From a source checkout we
    fall back to ../../web/out at the repo root.
    """
    bundled = Path(__file__).parent / "web_out"
    if bundled.exists():
        return bundled
    return Path(__file__).parent.parent.parent / "web" / "out"


STATIC_DIR = _resolve_static_dir()

_write_executor = ThreadPoolExecutor(max_workers=1)
_write_lock = asyncio.Lock()
_read_executor = ThreadPoolExecutor(max_workers=8)


# ---------------------------------------------------------------------------
# WebSocket worker hub
# ---------------------------------------------------------------------------


class ConnectedWorker:
    """Represents a worker connected via WebSocket."""

    def __init__(self, ws: WebSocket, worker_name: str, machine_name: str):
        self.ws = ws
        self.worker_name = worker_name
        self.machine_name = machine_name
        self.connected_at = datetime.now(timezone.utc).isoformat()
        self.last_heartbeat = datetime.now(timezone.utc).isoformat()
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._event_subscribers: list[asyncio.Queue] = []

    def subscribe_events(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._event_subscribers.append(q)
        return q

    def unsubscribe_events(self, q: asyncio.Queue) -> None:
        self._event_subscribers = [s for s in self._event_subscribers if s is not q]

    def push_event(self, event: dict) -> None:
        for q in self._event_subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


class WorkerHub:
    """Manages WebSocket connections from fleet workers."""

    def __init__(self):
        self._workers: dict[str, ConnectedWorker] = {}
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket, worker_name: str, machine_name: str) -> ConnectedWorker:
        async with self._lock:
            old = self._workers.get(worker_name)
            if old:
                try:
                    await old.ws.close()
                except Exception:
                    pass
            cw = ConnectedWorker(ws, worker_name, machine_name)
            self._workers[worker_name] = cw
            return cw

    async def unregister(self, worker_name: str) -> None:
        async with self._lock:
            self._workers.pop(worker_name, None)

    def get(self, worker_name: str) -> ConnectedWorker | None:
        return self._workers.get(worker_name)

    def connected_names(self) -> list[str]:
        return list(self._workers.keys())

    def all_workers(self) -> list[ConnectedWorker]:
        return list(self._workers.values())


_hub = WorkerHub()


# ---------------------------------------------------------------------------
# WebSocket machine hub — external machines connect here
# ---------------------------------------------------------------------------


class ConnectedMachine:
    """Represents an external machine connected via WebSocket."""

    def __init__(self, ws: WebSocket, machine_name: str, system_info: dict):
        self.ws = ws
        self.machine_name = machine_name
        self.system_info = system_info
        self.connected_at = datetime.now(timezone.utc).isoformat()
        self.last_heartbeat = datetime.now(timezone.utc).isoformat()
        self.worker_names: list[str] = []
        self._pending_responses: dict[str, asyncio.Future] = {}

    async def send_command(self, command: dict, timeout: float = 60.0) -> dict:
        request_id = uuid.uuid4().hex[:8]
        command["request_id"] = request_id
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_responses[request_id] = future
        try:
            await self.ws.send_json(command)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"error": "Machine did not respond in time"}
        finally:
            self._pending_responses.pop(request_id, None)


class MachineHub:
    """Manages WebSocket connections from external machines."""

    def __init__(self):
        self._machines: dict[str, ConnectedMachine] = {}
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket, machine_name: str, system_info: dict) -> ConnectedMachine:
        async with self._lock:
            old = self._machines.get(machine_name)
            if old:
                try:
                    await old.ws.close()
                except Exception:
                    pass
            cm = ConnectedMachine(ws, machine_name, system_info)
            self._machines[machine_name] = cm
            return cm

    async def unregister(self, machine_name: str) -> None:
        async with self._lock:
            self._machines.pop(machine_name, None)

    def get(self, machine_name: str) -> ConnectedMachine | None:
        return self._machines.get(machine_name)

    def connected_names(self) -> list[str]:
        return list(self._machines.keys())

    def all_machines(self) -> list[ConnectedMachine]:
        return list(self._machines.values())


_machine_hub = MachineHub()


# ---------------------------------------------------------------------------
# Background task registry
# ---------------------------------------------------------------------------

class TaskInfo(BaseModel):
    id: str
    operation: str
    worker_name: str
    status: str = "running"
    started_at: str
    finished_at: str | None = None
    error: str | None = None


_tasks: dict[str, TaskInfo] = {}
_MAX_FINISHED_TASKS = 200


def _prune_tasks() -> None:
    finished = [(tid, t) for tid, t in _tasks.items() if t.finished_at]
    if len(finished) <= _MAX_FINISHED_TASKS:
        return
    finished.sort(key=lambda x: x[1].finished_at or "")
    for tid, _ in finished[: len(finished) - _MAX_FINISHED_TASKS]:
        del _tasks[tid]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SpawnRequest(BaseModel):
    name: str
    machine_name: str | None = None
    provider: str | None = None
    model: str | None = None
    vm_type: str | None = None
    instance_type: str | None = None
    repos: list[str] | None = None
    region: str | None = None


class AskRequest(BaseModel):
    prompt: str


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_server_token() -> str:
    env_token = os.environ.get("FLEET_API_TOKEN", "")
    if env_token:
        return env_token
    try:
        config = FleetConfig.load()
        return config.server.token
    except FileNotFoundError:
        return ""


async def _verify_token(request: Request) -> None:
    token = _get_server_token()
    if not token:
        return
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        bearer = auth_header[7:]
        if hmac.compare_digest(bearer, token):
            return
    query_token = request.query_params.get("token", "")
    if query_token and hmac.compare_digest(query_token, token):
        return
    raise HTTPException(status_code=401, detail="Invalid or missing token")


def _verify_token_sync(token_value: str) -> bool:
    expected = _get_server_token()
    if not expected:
        return True
    return hmac.compare_digest(token_value, expected)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run_write(func):
    async with _write_lock:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_write_executor, func)


async def _run_read(func):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_read_executor, func)


async def _run_background_task(task_id: str, func, cleanup_worker: str | None = None):
    try:
        await _run_write(func)
        _tasks[task_id].status = "completed"
    except Exception as e:
        _tasks[task_id].status = "failed"
        _tasks[task_id].error = str(e)
        if cleanup_worker:
            try:
                state = FleetState.load()
                if cleanup_worker in state.workers:
                    del state.workers[cleanup_worker]
                    state.save()
            except Exception:
                pass
    finally:
        _tasks[task_id].finished_at = datetime.now(timezone.utc).isoformat()
        _prune_tasks()


async def _send_command_to_worker(worker_name: str, command: dict) -> dict:
    """Send a command to a worker via WebSocket."""
    cw = _hub.get(worker_name)
    if not cw:
        return {"error": f"Worker '{worker_name}' is not connected"}

    request_id = uuid.uuid4().hex[:8]
    command["request_id"] = request_id
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    cw._pending_responses[request_id] = future
    try:
        await cw.ws.send_json(command)
        return await asyncio.wait_for(future, timeout=30.0)
    except asyncio.TimeoutError:
        return {"error": "Worker did not respond within 30s"}
    finally:
        cw._pending_responses.pop(request_id, None)


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------


def generate_server_token() -> str:
    """Generate a new server token and save it to config."""
    token = secrets.token_urlsafe(32)
    try:
        config = FleetConfig.load()
    except FileNotFoundError:
        config = FleetConfig()
    config.server.token = token
    config.save()
    return token


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_server_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        _write_executor.shutdown(wait=False)
        _read_executor.shutdown(wait=False)

    app = FastAPI(title="Claude Fleet Server", version="1.0.0", lifespan=lifespan)

    # ------------------------------------------------------------------
    # Dashboard + Static
    # ------------------------------------------------------------------

    @app.get("/")
    async def dashboard():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/manifest.json")
    async def manifest():
        manifest_path = STATIC_DIR / "manifest.json"
        if manifest_path.exists():
            return FileResponse(manifest_path)
        raise HTTPException(status_code=404)

    @app.get("/favicon.ico")
    async def favicon():
        favicon_path = STATIC_DIR / "favicon.ico"
        if favicon_path.exists():
            return FileResponse(favicon_path)
        raise HTTPException(status_code=404)

    next_dir = STATIC_DIR / "_next"
    next_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/_next", StaticFiles(directory=str(next_dir)), name="next-static")

    # ------------------------------------------------------------------
    # WebSocket hub — workers connect here
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def worker_websocket(ws: WebSocket):
        await ws.accept()

        # First message must be registration
        try:
            reg = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
        except (asyncio.TimeoutError, Exception):
            await ws.close(code=4001, reason="Registration timeout")
            return

        if reg.get("type") != "register":
            await ws.close(code=4002, reason="First message must be register")
            return

        token = reg.get("token", "")
        if not _verify_token_sync(token):
            await ws.close(code=4003, reason="Invalid token")
            return

        worker_name = reg.get("worker_name", "")
        machine_name = reg.get("machine_name", "")
        if not worker_name:
            await ws.close(code=4004, reason="Missing worker_name")
            return

        reg_session_id = reg.get("session_id", "")
        reg_cwd = reg.get("cwd", "")
        reg_model = reg.get("model", "")
        reg_ssh_host = reg.get("ssh_host", "")
        reg_ssh_user = reg.get("ssh_user", "")

        cw = await _hub.register(ws, worker_name, machine_name)

        # Update state to reflect connected worker (auto-create if started via `cfleet agent`)
        try:
            state = FleetState.load()
            from cfleet.config import WorkerState as WS, MachineState as MS

            if worker_name in state.workers:
                w = state.workers[worker_name]
                if w.status in ("spawning", "provisioning", "errored"):
                    w.status = "idle"
            else:
                w = WS(
                    name=worker_name,
                    machine_name=machine_name,
                    status="idle",
                    local_mode=True,
                )
                state.add_worker(w)
            if reg_session_id:
                w.session_id = reg_session_id
            if reg_cwd:
                w.cwd = reg_cwd
            if reg_model:
                w.model = reg_model

            # Register / refresh the machine record so `cfleet attach` can find SSH info.
            # Only touch ssh fields / status when the machine is "external" (BYO via
            # `cfleet join`/`cfleet agent`). Managed machines (gcp/azure/devcontainer)
            # are owned by the provisioner — never overwrite their SSH or flip status.
            if machine_name:
                if machine_name not in state.machines:
                    state.add_machine(MS(
                        name=machine_name,
                        provider="external",
                        status="ready",
                        ssh_host=reg_ssh_host,
                        ssh_user=reg_ssh_user,
                    ))
                else:
                    m = state.machines[machine_name]
                    if m.provider in ("", "external"):
                        if reg_ssh_host:
                            m.ssh_host = reg_ssh_host
                        if reg_ssh_user:
                            m.ssh_user = reg_ssh_user
                        m.status = "ready"
                if worker_name not in state.machines[machine_name].worker_names:
                    state.machines[machine_name].worker_names.append(worker_name)

            state.save()
        except Exception:
            pass

        await ws.send_json({"type": "registered", "worker_name": worker_name})

        # Message loop
        try:
            while True:
                data = await ws.receive_json()
                msg_type = data.get("type")

                if msg_type == "heartbeat":
                    cw.last_heartbeat = datetime.now(timezone.utc).isoformat()
                    await ws.send_json({"type": "heartbeat_ack"})

                elif msg_type == "response":
                    request_id = data.get("request_id")
                    if request_id and request_id in cw._pending_responses:
                        cw._pending_responses[request_id].set_result(data.get("data", {}))

                elif msg_type == "event":
                    cw.push_event({"kind": "event", "payload": data.get("data", {})})

                elif msg_type == "status_update":
                    new_status = data.get("status")
                    try:
                        state = FleetState.load()
                        if worker_name in state.workers:
                            if new_status in ("idle", "working", "errored"):
                                state.workers[worker_name].status = new_status
                                state.save()
                    except Exception:
                        pass
                    # Also push to SSE subscribers so the dashboard can pulse.
                    cw.push_event({"kind": "status", "status": new_status})

        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            await _hub.unregister(worker_name)
            try:
                state = FleetState.load()
                if worker_name in state.workers:
                    w = state.workers[worker_name]
                    if w.local_mode:
                        state.remove_worker(worker_name)
                    else:
                        w.status = "stopped"
                    state.save()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # WebSocket hub — external machines connect here
    # ------------------------------------------------------------------

    @app.websocket("/ws/machine")
    async def machine_websocket(ws: WebSocket):
        await ws.accept()

        try:
            reg = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
        except (asyncio.TimeoutError, Exception):
            await ws.close(code=4001, reason="Registration timeout")
            return

        if reg.get("type") != "register":
            await ws.close(code=4002, reason="First message must be register")
            return

        token = reg.get("token", "")
        if not _verify_token_sync(token):
            await ws.close(code=4003, reason="Invalid token")
            return

        machine_name = reg.get("machine_name", "")
        if not machine_name:
            await ws.close(code=4004, reason="Missing machine_name")
            return

        system_info = reg.get("system_info", {})
        worker_names = reg.get("worker_names", [])

        cm = await _machine_hub.register(ws, machine_name, system_info)
        cm.worker_names = worker_names

        # Create or update machine in state
        try:
            state = FleetState.load()
            if machine_name not in state.machines:
                from cfleet.config import MachineState
                machine = MachineState(
                    name=machine_name,
                    hostname=system_info.get("hostname", ""),
                    os_info=system_info.get("os", ""),
                    status="ready",
                )
                state.add_machine(machine)
            else:
                machine = state.machines[machine_name]
                machine.hostname = system_info.get("hostname", "")
                machine.os_info = system_info.get("os", "")
                machine.status = "ready"
            state.save()
        except Exception:
            pass

        await ws.send_json({"type": "registered", "machine_name": machine_name})

        try:
            while True:
                data = await ws.receive_json()
                msg_type = data.get("type")

                if msg_type == "heartbeat":
                    cm.last_heartbeat = datetime.now(timezone.utc).isoformat()
                    cm.worker_names = data.get("worker_names", cm.worker_names)
                    await ws.send_json({"type": "heartbeat_ack"})

                elif msg_type == "response":
                    request_id = data.get("request_id")
                    if request_id and request_id in cm._pending_responses:
                        cm._pending_responses[request_id].set_result(data.get("data", {}))

                elif msg_type == "pong":
                    request_id = data.get("request_id")
                    if request_id and request_id in cm._pending_responses:
                        cm._pending_responses[request_id].set_result({"ok": True})

        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            await _machine_hub.unregister(machine_name)
            try:
                state = FleetState.load()
                if machine_name in state.machines:
                    state.machines[machine_name].status = "disconnected"
                    state.save()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Server info
    # ------------------------------------------------------------------

    @app.get("/api/server/info")
    async def server_info(request: Request):
        await _verify_token(request)
        connected = _hub.connected_names()
        connected_machines = _machine_hub.connected_names()
        return {
            "version": "1.0.0",
            "connected_workers": connected,
            "connected_count": len(connected),
            "connected_machines": connected_machines,
            "connected_machine_count": len(connected_machines),
        }

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    @app.get("/api/config")
    async def get_config(request: Request):
        await _verify_token(request)
        from cfleet.config import DEFAULT_SKUS, PROVIDER_DEFAULTS
        config = FleetConfig.load()
        return {
            "provider": config.cloud.provider,
            "model": config.model,
            "region": config.resolve_region(),
            "ssh_user": config.resolve_ssh_user(),
            "instance_type": config.resolve_instance_type(),
            "vm_type": config.cloud.vm_type.value,
            "providers": {
                "azure": {
                    "region": PROVIDER_DEFAULTS["azure"]["region"],
                    "instance_type": PROVIDER_DEFAULTS["azure"]["instance_type"],
                    "skus": {k.value: v for k, v in DEFAULT_SKUS.get("azure", {}).items()},
                },
                "gcp": {
                    "region": PROVIDER_DEFAULTS["gcp"]["region"],
                    "instance_type": PROVIDER_DEFAULTS["gcp"]["instance_type"],
                    "skus": {k.value: v for k, v in DEFAULT_SKUS.get("gcp", {}).items()},
                },
                "devcontainer": {
                    "region": "local",
                    "instance_type": "docker",
                    "skus": {},
                },
            },
        }

    # ------------------------------------------------------------------
    # Machines
    # ------------------------------------------------------------------

    @app.get("/api/machines")
    async def list_machines(request: Request):
        await _verify_token(request)
        state = FleetState.load()
        connected_machines = set(_machine_hub.connected_names())
        result = []
        for m in state.machines.values():
            d = m.model_dump()
            d["connected"] = m.name in connected_machines
            result.append(d)
        return result

    @app.post("/api/machines/{name}/spawn")
    async def spawn_on_machine(name: str, request: Request):
        """Send a spawn command to an external machine via its WebSocket."""
        await _verify_token(request)
        body = await request.json()
        worker_name = body.get("worker_name", "")
        if not worker_name:
            raise HTTPException(status_code=400, detail="Missing worker_name")

        cm = _machine_hub.get(name)
        if not cm:
            raise HTTPException(status_code=404, detail=f"Machine '{name}' is not connected")

        result = await cm.send_command({
            "type": "spawn_worker",
            "worker_name": worker_name,
            "model": body.get("model", ""),
            "repos": body.get("repos", []),
            "cwd": body.get("cwd", ""),
        })
        return result

    @app.post("/api/machines/{name}/kill")
    async def kill_on_machine(name: str, request: Request):
        """Send a kill command to an external machine via its WebSocket."""
        await _verify_token(request)
        body = await request.json()
        worker_name = body.get("worker_name", "")
        if not worker_name:
            raise HTTPException(status_code=400, detail="Missing worker_name")

        cm = _machine_hub.get(name)
        if not cm:
            raise HTTPException(status_code=404, detail=f"Machine '{name}' is not connected")

        result = await cm.send_command({
            "type": "kill_worker",
            "worker_name": worker_name,
        })
        return result

    # ------------------------------------------------------------------
    # Workers — reads
    # ------------------------------------------------------------------

    @app.get("/api/workers")
    async def list_workers(request: Request):
        await _verify_token(request)
        state = FleetState.load()
        workers = []
        connected = set(_hub.connected_names())
        for w in state.workers.values():
            d = w.model_dump()
            d["connected"] = w.name in connected
            workers.append(d)
        return workers

    @app.get("/api/workers/{name}")
    async def get_worker(name: str, request: Request):
        await _verify_token(request)
        state = FleetState.load()
        if name not in state.workers:
            raise HTTPException(status_code=404, detail=f"Worker '{name}' not found.")
        worker = state.workers[name]
        machine = state.machines.get(worker.machine_name)
        connected = name in _hub.connected_names()

        info = {
            "name": worker.name,
            "status": worker.status,
            "machine_name": worker.machine_name,
            "relay_port": worker.relay_port,
            "model": worker.model,
            "repos": worker.repos,
            "created_at": worker.created_at,
            "last_prompt": worker.last_prompt,
            "last_prompt_at": worker.last_prompt_at,
            "session_id": worker.session_id,
            "cwd": worker.cwd,
            "connected": connected,
        }
        if machine:
            info["provider"] = machine.provider
            info["machine_ip"] = machine.ip
            info["ssh_host"] = machine.ssh_host
            info["ssh_user"] = machine.ssh_user

        # Get live relay status from the connected worker
        if connected:
            try:
                result = await _send_command_to_worker(name, {"type": "status"})
                info["relay_alive"] = "error" not in result
                info["message_count"] = result.get("message_count", 0)
            except Exception:
                info["relay_alive"] = False
        else:
            info["relay_alive"] = False

        return info

    # ------------------------------------------------------------------
    # Workers — writes
    # ------------------------------------------------------------------

    @app.post("/api/workers")
    async def spawn_worker(req: SpawnRequest, request: Request):
        await _verify_token(request)
        task_id = uuid.uuid4().hex[:8]
        _tasks[task_id] = TaskInfo(
            id=task_id,
            operation="spawn",
            worker_name=req.name,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        resolved_vm_type = VMType(req.vm_type) if req.vm_type else None

        asyncio.create_task(
            _run_background_task(
                task_id,
                lambda: FleetEngine().spawn(
                    name=req.name,
                    machine_name=req.machine_name,
                    repos=req.repos,
                    model=req.model,
                    vm_type=resolved_vm_type,
                    instance_type=req.instance_type,
                    region=req.region,
                    provider=req.provider,
                ),
                cleanup_worker=req.name,
            )
        )
        return {"task_id": task_id}

    @app.delete("/api/workers/{name}")
    async def kill_worker(name: str, request: Request, purge: bool = False):
        await _verify_token(request)
        task_id = uuid.uuid4().hex[:8]
        _tasks[task_id] = TaskInfo(
            id=task_id,
            operation="kill",
            worker_name=name,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        asyncio.create_task(
            _run_background_task(
                task_id,
                lambda name=name, purge=purge: FleetEngine().kill(name, purge=purge),
            )
        )
        return {"task_id": task_id}

    # ------------------------------------------------------------------
    # Worker commands — routed via WebSocket or SSH fallback
    # ------------------------------------------------------------------

    @app.post("/api/workers/{name}/ask")
    async def ask_worker(name: str, req: AskRequest, request: Request):
        await _verify_token(request)
        try:
            result = await _send_command_to_worker(name, {
                "type": "ask",
                "prompt": req.prompt,
            })

            if "error" in result:
                raise HTTPException(status_code=503, detail=result["error"])

            # Update state
            try:
                state = FleetState.load()
                if name in state.workers:
                    w = state.workers[name]
                    w.last_prompt = req.prompt
                    w.last_prompt_at = datetime.now(timezone.utc).isoformat()
                    w.status = "working"
                    state.save()
            except Exception:
                pass

            return result
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.post("/api/workers/{name}/interrupt")
    async def interrupt_worker(name: str, request: Request):
        await _verify_token(request)
        try:
            result = await _send_command_to_worker(name, {"type": "interrupt"})
            if "error" in result:
                raise HTTPException(status_code=503, detail=result["error"])
            return result
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    @app.get("/api/workers/{name}/messages")
    async def get_messages(name: str, request: Request, offset: int = 0, limit: int = 200):
        await _verify_token(request)
        try:
            result = await _send_command_to_worker(name, {
                "type": "messages",
                "offset": offset,
                "limit": limit,
            })
            return result
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception:
            return {"messages": [], "error": "Worker unreachable"}

    # ------------------------------------------------------------------
    # Usage
    # ------------------------------------------------------------------

    @app.get("/api/workers/{name}/usage")
    async def get_worker_usage(name: str, request: Request):
        await _verify_token(request)
        try:
            result = await _send_command_to_worker(name, {"type": "status"})
            state = FleetState.load()
            worker = state.get_worker(name)
            return {
                "name": name,
                "model": worker.model,
                "total_input_tokens": result.get("total_input_tokens", 0),
                "total_output_tokens": result.get("total_output_tokens", 0),
                "total_cost_usd": result.get("total_cost_usd", 0.0),
            }
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

    # ------------------------------------------------------------------
    # Logs — SSE stream via worker events
    # ------------------------------------------------------------------

    @app.get("/api/workers/{name}/logs/snapshot")
    async def log_snapshot(name: str, request: Request, lines: int = 100):
        await _verify_token(request)
        try:
            result = await _send_command_to_worker(name, {
                "type": "messages",
                "offset": 0,
                "limit": lines,
            })
            from cfleet.relay_client import format_message
            formatted = []
            for msg in result.get("messages", []):
                f = format_message(msg)
                if f.strip():
                    formatted.append(f)
            return {"lines": "\n".join(formatted)}
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/api/workers/{name}/logs")
    async def stream_logs(name: str, request: Request):
        await _verify_token(request)
        try:
            FleetState.load().get_worker(name)
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=404, detail=str(e))

        return EventSourceResponse(_log_generator(name))

    async def _log_generator(name: str) -> AsyncGenerator[dict, None]:
        from cfleet.relay_client import format_message

        # Send existing messages first
        try:
            result = await _send_command_to_worker(name, {
                "type": "messages", "offset": 0, "limit": 200,
            })
            messages = result.get("messages", [])
            if messages:
                formatted = []
                for msg in messages:
                    f = format_message(msg)
                    if f.strip():
                        formatted.append(f)
                if formatted:
                    yield {
                        "event": "logs",
                        "data": json.dumps({"content": "\n".join(formatted)}),
                    }
        except Exception:
            pass

        # Stream new events from WebSocket-connected worker
        cw = _hub.get(name)
        if cw:
            queue = cw.subscribe_events()
            try:
                while True:
                    try:
                        envelope = await asyncio.wait_for(queue.get(), timeout=30.0)
                        kind = envelope.get("kind")
                        if kind == "status":
                            yield {
                                "event": "status",
                                "data": json.dumps({"status": envelope.get("status")}),
                            }
                        elif kind == "event":
                            payload = envelope.get("payload", {})
                            formatted = format_message(payload) if "type" in payload else str(payload)
                            if formatted.strip():
                                yield {
                                    "event": "message",
                                    "data": json.dumps({"message": payload, "formatted": formatted}),
                                }
                    except asyncio.TimeoutError:
                        yield {"event": "keepalive", "data": "{}"}
            finally:
                cw.unsubscribe_events(queue)
        else:
            yield {"event": "info", "data": json.dumps({"message": "Worker not connected via WebSocket, use SSH logs"})}

    # ------------------------------------------------------------------
    # GitHub token broker
    # ------------------------------------------------------------------

    @app.post("/api/github/token")
    async def github_token(request: Request):
        """Generate a scoped GitHub installation token for the requesting worker.

        Called by the credential helper on worker machines. Auth is via the
        fleet bearer token. The worker_name is identified from the request body
        or from the WebSocket registration.
        """
        await _verify_token(request)
        body = await request.json()
        worker_name = body.get("worker_name", "")
        if not worker_name:
            raise HTTPException(status_code=400, detail="Missing worker_name")

        from cfleet.github import generate_installation_token, GitHubTokenError
        try:
            config = FleetConfig.load()
            state = FleetState.load()
            result = await _run_read(
                lambda: generate_installation_token(config, worker_name, state)
            )
            return result
        except GitHubTokenError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/api/github/level/{name}")
    async def get_github_level(name: str, request: Request):
        await _verify_token(request)
        state = FleetState.load()
        if name not in state.workers:
            raise HTTPException(status_code=404, detail=f"Worker '{name}' not found.")
        return {"worker_name": name, "github_level": state.workers[name].github_level}

    @app.put("/api/github/level/{name}")
    async def set_github_level(name: str, request: Request):
        await _verify_token(request)
        body = await request.json()
        level_str = body.get("level", "")
        try:
            level = GitHubLevel(level_str)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid level '{level_str}'. Choose: none, read, triage, write",
            )

        state = FleetState.load()
        if name not in state.workers:
            raise HTTPException(status_code=404, detail=f"Worker '{name}' not found.")
        old_level = state.workers[name].github_level
        state.workers[name].github_level = level.value
        state.save()

        result = {
            "worker_name": name,
            "github_level": level.value,
            "previous_level": old_level,
        }

        if level == GitHubLevel.WRITE:
            from cfleet.github import warn_unprotected_repos
            warnings = warn_unprotected_repos(FleetConfig.load(), state.workers[name].repos)
            if warnings:
                result["branch_protection_warnings"] = warnings

        return result

    @app.get("/api/github/log")
    async def github_log(request: Request, worker_name: str = "", limit: int = 50):
        await _verify_token(request)
        state = FleetState.load()
        entries = state.github_token_log
        if worker_name:
            entries = [e for e in entries if e.worker_name == worker_name]
        entries = entries[-limit:]
        return [e.model_dump() for e in entries]

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    @app.get("/api/tasks")
    async def list_tasks(request: Request):
        await _verify_token(request)
        cutoff = datetime.now(timezone.utc).timestamp() - 3600
        to_prune = [
            tid for tid, t in _tasks.items()
            if t.finished_at and datetime.fromisoformat(t.finished_at).timestamp() < cutoff
        ]
        for tid in to_prune:
            del _tasks[tid]
        return list(_tasks.values())

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str, request: Request):
        await _verify_token(request)
        task = _tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return task

    return app
