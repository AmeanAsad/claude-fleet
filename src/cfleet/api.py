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

STATIC_DIR = Path(__file__).parent / "static"

# Mutating operations (spawn, kill, ask) serialize through this lock+executor
# to protect state.json writes.  Read-only operations bypass it entirely.
_write_executor = ThreadPoolExecutor(max_workers=1)
_write_lock = asyncio.Lock()

# Read-only operations (logs, status checks) run in a separate pool so
# they never block behind a spawn/kill and can run concurrently per worker.
_read_executor = ThreadPoolExecutor(max_workers=8)

# Background task registry: task_id -> TaskInfo
# Pruned on every write to prevent unbounded growth.
_tasks: dict[str, TaskInfo] = {}
_MAX_FINISHED_TASKS = 200

# Event store for HTTP hooks (Phase 2)
_events: EventStore | None = None


def _get_event_store() -> EventStore:
    global _events
    if _events is None:
        from cfleet.events import EventStore
        _events = EventStore()
    return _events


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
    """Run a read-only call in the concurrent read pool."""
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


async def _run_background_task(task_id: str, func, cleanup_worker: str | None = None):
    """Run a long engine operation and update the task registry on completion.

    If cleanup_worker is set and the task fails, remove the worker from state
    so it doesn't linger as a zombie.
    """
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


def _make_engine_and_worker(worker_name: str):
    """Create a FleetEngine and get the worker + connection."""
    engine = FleetEngine()
    worker = engine.state.get_worker(worker_name)
    return engine, worker


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        _write_executor.shutdown(wait=False)
        _read_executor.shutdown(wait=False)

    app = FastAPI(title="Claude Fleet", version="0.2.0", lifespan=lifespan)

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
    # Workers — reads (no lock, concurrent via _read_executor)
    # ------------------------------------------------------------------

    @app.get("/api/config")
    async def get_config(request: Request):
        await _verify_token(request)
        from cfleet.config import DEFAULT_SKUS, PROVIDER_DEFAULTS
        config = FleetConfig.load()
        provider = config.cloud.provider
        return {
            "provider": provider,
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

    @app.get("/api/workers")
    async def list_workers(request: Request):
        await _verify_token(request)
        state = FleetState.load()
        return [w.model_dump() for w in state.workers.values()]

    @app.get("/api/workers/{name}")
    async def get_worker(name: str, request: Request):
        await _verify_token(request)

        def _status():
            engine = FleetEngine()
            return engine.status(name)

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
                    provider=req.provider,
                ),
                cleanup_worker=req.name,
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

    @app.post("/api/workers/{name}/interrupt")
    async def interrupt_worker(name: str, request: Request):
        await _verify_token(request)
        try:
            await _run_write(
                lambda: FleetEngine().interrupt(name)
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"ok": True}

    # ------------------------------------------------------------------
    # Messages — structured conversation history (relay workers)
    # ------------------------------------------------------------------

    @app.get("/api/workers/{name}/messages")
    async def get_messages(name: str, request: Request, offset: int = 0, limit: int = 200):
        await _verify_token(request)

        def _read():
            return FleetEngine().messages(name, offset=offset, limit=limit)

        try:
            return await _run_read(_read)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception:
            return {"messages": [], "error": "Worker relay unreachable"}

    # ------------------------------------------------------------------
    # Usage / cost tracking
    # ------------------------------------------------------------------

    @app.get("/api/workers/{name}/usage")
    async def get_worker_usage(name: str, request: Request):
        await _verify_token(request)

        def _read():
            engine = FleetEngine()
            worker = engine.state.get_worker(name)
            info = {"name": name, "model": worker.model}
            if worker.communication_mode == "relay":
                try:
                    relay = engine._get_relay(worker)
                    status = relay.get_status_sync()
                    info["total_input_tokens"] = status.get("total_input_tokens", 0)
                    info["total_output_tokens"] = status.get("total_output_tokens", 0)
                    info["total_cache_read_tokens"] = status.get("total_cache_read_tokens", 0)
                    info["total_cache_creation_tokens"] = status.get("total_cache_creation_tokens", 0)
                    info["total_cost_usd"] = status.get("total_cost_usd", 0.0)
                except Exception:
                    info["error"] = "relay unreachable"
            else:
                info["total_input_tokens"] = worker.total_input_tokens
                info["total_output_tokens"] = worker.total_output_tokens
                info["total_cost_usd"] = worker.total_cost_usd
            return info

        try:
            return await _run_read(_read)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/api/fleet/usage")
    async def get_fleet_usage(request: Request):
        await _verify_token(request)

        def _read():
            engine = FleetEngine()
            totals = {
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cost_usd": 0.0,
                "worker_count": len(engine.state.workers),
                "workers": {},
            }
            for name, worker in engine.state.workers.items():
                w_usage = {"model": worker.model, "total_cost_usd": 0.0}
                if worker.communication_mode == "relay":
                    try:
                        relay = engine._get_relay(worker)
                        status = relay.get_status_sync()
                        w_usage["total_input_tokens"] = status.get("total_input_tokens", 0)
                        w_usage["total_output_tokens"] = status.get("total_output_tokens", 0)
                        w_usage["total_cost_usd"] = status.get("total_cost_usd", 0.0)
                    except Exception:
                        w_usage["error"] = "relay unreachable"
                else:
                    w_usage["total_input_tokens"] = worker.total_input_tokens
                    w_usage["total_output_tokens"] = worker.total_output_tokens
                    w_usage["total_cost_usd"] = worker.total_cost_usd
                totals["workers"][name] = w_usage
                totals["total_input_tokens"] += w_usage.get("total_input_tokens", 0)
                totals["total_output_tokens"] += w_usage.get("total_output_tokens", 0)
                totals["total_cost_usd"] += w_usage.get("total_cost_usd", 0.0)
            return totals

        return await _run_read(_read)

    # ------------------------------------------------------------------
    # Logs — SSE stream (relay or legacy tmux)
    # ------------------------------------------------------------------

    @app.get("/api/workers/{name}/logs/snapshot")
    async def log_snapshot(name: str, request: Request, lines: int = 100):
        await _verify_token(request)

        def _read():
            engine = FleetEngine()
            worker = engine.state.get_worker(name)
            if engine._uses_relay(worker):
                result = engine.messages(name, limit=lines)
                from cfleet.relay_client import format_message
                formatted = []
                for msg in result.get("messages", []):
                    f = format_message(msg)
                    if f.strip():
                        formatted.append(f)
                return "\n".join(formatted)
            else:
                conn = engine._get_conn(worker)
                try:
                    return conn.read_logs(lines)
                finally:
                    conn.close()

        try:
            output = await _run_read(_read)
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"lines": output}

    @app.get("/api/workers/{name}/logs")
    async def stream_logs(name: str, request: Request):
        await _verify_token(request)

        # Validate worker exists
        try:
            engine = FleetEngine()
            worker = engine.state.get_worker(name)
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=404, detail=str(e))

        uses_relay = engine._uses_relay(worker)

        if uses_relay:
            return EventSourceResponse(_relay_log_generator(name))
        else:
            return EventSourceResponse(_tmux_log_generator(name))

    async def _relay_log_generator(name: str) -> AsyncGenerator[dict, None]:
        """Stream structured messages from a relay worker."""
        from cfleet.relay_client import format_message
        engine = FleetEngine()
        worker = engine.state.get_worker(name)
        relay = engine._get_relay(worker)

        # Send existing messages first
        try:
            result = relay.get_messages_sync()
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

