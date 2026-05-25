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
