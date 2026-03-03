"""FastAPI web server — wraps FleetEngine for browser/phone control."""

from __future__ import annotations

import asyncio
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

from open_fleet.config import FleetConfig, FleetState, VMType
from open_fleet.engine import FleetEngine
from open_fleet.ssh import WorkerSSH

STATIC_DIR = Path(__file__).parent / "static"

# Single-thread executor ensures only one FleetEngine call at a time.
_executor = ThreadPoolExecutor(max_workers=1)
_engine_lock = asyncio.Lock()

# Background task registry: task_id -> TaskInfo
_tasks: dict[str, TaskInfo] = {}


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
    if credentials and credentials.credentials == token:
        return
    query_token = request.query_params.get("token", "")
    if query_token == token:
        return
    raise HTTPException(status_code=401, detail="Invalid or missing token")


# ---------------------------------------------------------------------------
# Engine helpers
# ---------------------------------------------------------------------------


async def _run_engine(func, *args, **kwargs):
    """Run a blocking FleetEngine call in the single-thread executor."""
    async with _engine_lock:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, lambda: func(*args, **kwargs))


async def _run_background_task(task_id: str, func, *args, **kwargs):
    """Run a long engine operation and update the task registry on completion."""
    try:
        await _run_engine(func, *args, **kwargs)
        _tasks[task_id].status = "completed"
    except Exception as e:
        _tasks[task_id].status = "failed"
        _tasks[task_id].error = str(e)
    finally:
        _tasks[task_id].finished_at = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        _executor.shutdown(wait=False)

    app = FastAPI(title="Open Fleet", version="0.1.0", lifespan=lifespan)

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    @app.get("/")
    async def dashboard():
        return FileResponse(STATIC_DIR / "index.html")

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    @app.get("/api/workers")
    async def list_workers(request: Request):
        await _verify_token(request)
        workers = await _run_engine(lambda: FleetEngine().list_workers())
        return [w.model_dump() for w in workers]

    @app.get("/api/workers/{name}")
    async def get_worker(name: str, request: Request):
        await _verify_token(request)
        try:
            info = await _run_engine(lambda: FleetEngine().status(name))
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return info

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
            await _run_engine(
                lambda: FleetEngine().ask(name, req.prompt)
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"ok": True}

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------

    @app.get("/api/workers/{name}/logs/snapshot")
    async def log_snapshot(name: str, request: Request, lines: int = 100):
        await _verify_token(request)
        try:
            config = FleetConfig.load()
            state = FleetState.load()
            worker = state.get_worker(name)
        except (FileNotFoundError, KeyError) as e:
            raise HTTPException(status_code=404, detail=str(e))

        if not worker.ip:
            raise HTTPException(status_code=400, detail="Worker has no IP yet")

        ssh = WorkerSSH(
            ip=worker.ip,
            user=config.cloud.ssh_user,
            key_path=str(config.resolve_ssh_key()),
        )
        try:
            output = await asyncio.to_thread(ssh.read_logs, lines)
        finally:
            ssh.close()
        return {"lines": output}

    @app.get("/api/workers/{name}/logs")
    async def stream_logs(name: str, request: Request):
        await _verify_token(request)

        try:
            config = FleetConfig.load()
            state = FleetState.load()
            worker = state.get_worker(name)
        except (FileNotFoundError, KeyError) as e:
            raise HTTPException(status_code=404, detail=str(e))

        if not worker.ip:
            raise HTTPException(status_code=400, detail="Worker has no IP yet")

        async def event_generator() -> AsyncGenerator[dict, None]:
            ssh = WorkerSSH(
                ip=worker.ip,
                user=config.cloud.ssh_user,
                key_path=str(config.resolve_ssh_key()),
            )
            seen_content = ""
            try:
                while True:
                    try:
                        output = await asyncio.to_thread(ssh.read_logs, 100)
                        if output != seen_content:
                            seen_content = output
                            yield {"event": "logs", "data": json.dumps({"content": output})}

                        is_idle = await asyncio.to_thread(ssh.is_claude_idle)
                        yield {
                            "event": "status",
                            "data": json.dumps({"idle": is_idle}),
                        }
                    except Exception as e:
                        yield {"event": "error", "data": json.dumps({"error": str(e)})}

                    await asyncio.sleep(3.0)
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
