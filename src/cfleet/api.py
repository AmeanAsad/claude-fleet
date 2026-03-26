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


