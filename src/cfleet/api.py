"""FastAPI web server — wraps FleetEngine for browser/phone control."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request, Security
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from cfleet.config import FleetConfig, FleetState, VMType
from cfleet.engine import FleetEngine
from cfleet.ssh import WorkerSSH

STATIC_DIR = Path(__file__).parent / "static"

# Mutating operations (spawn, kill, ask) serialize through this lock+executor
# to protect state.json writes.  Read-only operations bypass it entirely.
_write_executor = ThreadPoolExecutor(max_workers=1)
_write_lock = asyncio.Lock()

# Read-only SSH operations (logs, status checks) run in a separate pool so
# they never block behind a spawn/kill and can run concurrently per worker.
_read_executor = ThreadPoolExecutor(max_workers=8)

# Background task registry: task_id -> TaskInfo
# Pruned on every write to prevent unbounded growth.
_tasks: dict[str, TaskInfo] = {}
_MAX_FINISHED_TASKS = 200


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TaskInfo(BaseModel):
    id: str
    operation: str  # "spawn" | "kill"
    worker_name: str
    status: str = "running"  # running | completed | failed
    started_at: str
    finished_at: str | None = None
    error: str | None = None


class SpawnRequest(BaseModel):
    name: str
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

_security = HTTPBearer(auto_error=False)


def _get_config_token() -> str:
    env_token = os.environ.get("FLEET_API_TOKEN", "")
    if env_token:
        return env_token
    try:
        config = FleetConfig.load()
        return config.api.token
    except FileNotFoundError:
        return ""


async def _verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_security),
) -> None:
    token = _get_config_token()
    if not token:
        return  # Auth disabled
    # Check header first, then query param (for SSE EventSource)
    if credentials and hmac.compare_digest(credentials.credentials, token):
        return
    query_token = request.query_params.get("token", "")
    if hmac.compare_digest(query_token, token):
        return
    raise HTTPException(status_code=401, detail="Invalid or missing token")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run_write(func):
    """Run a state-mutating FleetEngine call, serialized."""
    async with _write_lock:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_write_executor, func)


async def _run_read(func):
    """Run a read-only SSH/state call in the concurrent read pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_read_executor, func)


def _prune_tasks() -> None:
    """Remove oldest finished tasks if over the cap."""
    finished = [
        (tid, t) for tid, t in _tasks.items() if t.finished_at
    ]
    if len(finished) <= _MAX_FINISHED_TASKS:
        return
    finished.sort(key=lambda x: x[1].finished_at or "")
    for tid, _ in finished[: len(finished) - _MAX_FINISHED_TASKS]:
        del _tasks[tid]


async def _run_background_task(task_id: str, func):
    """Run a long engine operation and update the task registry on completion."""
    try:
        await _run_write(func)
        _tasks[task_id].status = "completed"
    except Exception as e:
        _tasks[task_id].status = "failed"
        _tasks[task_id].error = str(e)
    finally:
        _tasks[task_id].finished_at = datetime.now(timezone.utc).isoformat()
        _prune_tasks()


def _make_conn(worker_name: str):
    """Create a WorkerSSH or WorkerDocker for a given worker."""
    config = FleetConfig.load()
    state = FleetState.load()
    worker = state.get_worker(worker_name)
    if worker.provider == "devcontainer":
        if not worker.container_id:
            raise ValueError(f"Worker '{worker_name}' has no container ID yet")
        from cfleet.devcontainer import WorkerDocker
        return WorkerDocker(container_id=worker.container_id)
    if not worker.ip:
        raise ValueError(f"Worker '{worker_name}' has no IP yet")
    return WorkerSSH(
        ip=worker.ip,
        user=config.resolve_ssh_user(),
        key_path=str(config.resolve_ssh_key()),
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        _write_executor.shutdown(wait=False)
        _read_executor.shutdown(wait=False)

    app = FastAPI(title="Claude Fleet", version="0.1.0", lifespan=lifespan)

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    @app.get("/")
    async def dashboard():
        return FileResponse(STATIC_DIR / "index.html")

    # ------------------------------------------------------------------
    # Workers — reads (no lock, concurrent via _read_executor)
    # ------------------------------------------------------------------

    @app.get("/api/workers")
    async def list_workers(request: Request):
        await _verify_token(request)
        # Pure file read — no SSH, no lock
        state = FleetState.load()
        return [w.model_dump() for w in state.workers.values()]

    @app.get("/api/workers/{name}")
    async def get_worker(name: str, request: Request):
        await _verify_token(request)

        def _status():
            state = FleetState.load()
            worker = state.get_worker(name)
            info = worker.model_dump()
            reachable = (worker.ip or worker.container_id) and worker.status not in ("stopped", "spawning")
            if not reachable:
                info["tmux_alive"] = False
                info["uptime"] = "N/A"
                info["idle"] = False
                return info
            conn = _make_conn(name)
            try:
                combined = (
                    "tmux has-session -t claude 2>/dev/null && echo TMUX_OK || echo TMUX_DEAD; "
                    "uptime -p 2>/dev/null || echo unknown; "
                    "tmux capture-pane -t claude:code -p -S -5 2>/dev/null"
                )
                stdout, _, _ = conn.exec(combined)
                lines = stdout.splitlines()
                info["tmux_alive"] = len(lines) > 0 and lines[0] == "TMUX_OK"
                info["uptime"] = lines[1].strip() if len(lines) > 1 else "unknown"
                pane_lines = [l.strip() for l in lines[2:] if l.strip()]
                info["idle"] = any(
                    l == "\u276f" or l.startswith("\u276f ") for l in pane_lines
                )
            except Exception:
                info["tmux_alive"] = False
                info["uptime"] = "unknown"
                info["idle"] = False
            finally:
                conn.close()
            return info

        try:
            return await _run_read(_status)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

    # ------------------------------------------------------------------
    # Workers — writes (serialized via _write_lock)
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
                    repos=req.repos,
                    model=req.model,
                    vm_type=resolved_vm_type,
                    instance_type=req.instance_type,
                    region=req.region,
                ),
            )
        )
        return {"task_id": task_id}

    @app.delete("/api/workers/{name}")
    async def kill_worker(name: str, request: Request):
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
                lambda name=name: FleetEngine().kill(name),
            )
        )
        return {"task_id": task_id}

    @app.post("/api/workers/{name}/ask")
    async def ask_worker(name: str, req: AskRequest, request: Request):
        await _verify_token(request)
        try:
            await _run_write(
                lambda: FleetEngine().ask(name, req.prompt)
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"ok": True}

    # ------------------------------------------------------------------
    # Logs — read-only, concurrent per-worker SSH
    # ------------------------------------------------------------------

    @app.get("/api/workers/{name}/logs/snapshot")
    async def log_snapshot(name: str, request: Request, lines: int = 100):
        await _verify_token(request)

        def _read():
            ssh = _make_conn(name)
            try:
                return ssh.read_logs(lines)
            finally:
                ssh.close()

        try:
            output = await _run_read(_read)
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"lines": output}

    @app.get("/api/workers/{name}/logs")
    async def stream_logs(name: str, request: Request):
        await _verify_token(request)

        # Validate worker exists before starting SSE
        try:
            ssh = _make_conn(name)
            ssh.close()
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=404, detail=str(e))

        async def event_generator() -> AsyncGenerator[dict, None]:
            ssh = _make_conn(name)
            seen_content = ""
            try:
                while True:
                    try:
                        # Both calls reuse the same SSH connection
                        output, is_idle = await _run_read(
                            lambda: (ssh.read_logs(100), ssh.is_claude_idle())
                        )

                        if output != seen_content:
                            seen_content = output
                            yield {"event": "logs", "data": json.dumps({"content": output})}

                        yield {
                            "event": "status",
                            "data": json.dumps({"idle": is_idle}),
                        }
                    except Exception as e:
                        yield {"event": "error", "data": json.dumps({"error": str(e)})}
                        # Reconnect on SSH failure
                        ssh.close()
                        ssh = _make_conn(name)

                    await asyncio.sleep(2.0)
            finally:
                ssh.close()

        return EventSourceResponse(event_generator())

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    @app.get("/api/tasks")
    async def list_tasks(request: Request):
        await _verify_token(request)
        # Prune tasks finished more than 1 hour ago
        cutoff = datetime.now(timezone.utc).timestamp() - 3600
        to_prune = [
            tid
            for tid, t in _tasks.items()
            if t.finished_at
            and datetime.fromisoformat(t.finished_at).timestamp() < cutoff
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
