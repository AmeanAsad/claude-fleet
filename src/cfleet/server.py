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

