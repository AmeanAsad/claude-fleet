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
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from cfleet.config import FleetConfig, FleetState, GitHubLevel, VMType
from cfleet.engine import FleetEngine

STATIC_DIR = Path(__file__).parent / "static"

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

        cw = await _hub.register(ws, worker_name, machine_name)

        # Update state to reflect connected worker
        try:
            state = FleetState.load()
            if worker_name in state.workers:
                w = state.workers[worker_name]
                if w.status in ("spawning", "provisioning", "errored"):
                    w.status = "idle"
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
                    cw.push_event(data.get("data", {}))

                elif msg_type == "status_update":
                    try:
                        state = FleetState.load()
                        if worker_name in state.workers:
                            new_status = data.get("status")
                            if new_status in ("idle", "working", "errored"):
                                state.workers[worker_name].status = new_status
                                state.save()
                    except Exception:
                        pass

        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            await _hub.unregister(worker_name)

    # ------------------------------------------------------------------
    # Server info
    # ------------------------------------------------------------------

    @app.get("/api/server/info")
    async def server_info(request: Request):
        await _verify_token(request)
        connected = _hub.connected_names()
        return {
            "version": "1.0.0",
            "connected_workers": connected,
            "connected_count": len(connected),
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
        return [m.model_dump() for m in state.machines.values()]

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
            "connected": connected,
        }
        if machine:
            info["provider"] = machine.provider
            info["machine_ip"] = machine.ip

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
