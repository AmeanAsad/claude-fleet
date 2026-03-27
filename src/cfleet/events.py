"""Event store for HTTP hooks — ring buffer with pub/sub for SSE streaming."""

from __future__ import annotations

import asyncio
from collections import deque
from threading import Lock

from pydantic import BaseModel


class FleetEvent(BaseModel):
    worker_name: str
    event_type: str  # "tool_use", "stop", "session_start", etc.
    timestamp: str
    data: dict


class EventStore:
    """Thread-safe ring buffer of fleet events with async pub/sub.

    Stores the last `max_events` events per worker and supports
    async subscribers for SSE streaming.
    """

    def __init__(self, max_events: int = 1000):
        self._max = max_events
        self._events: deque[FleetEvent] = deque(maxlen=max_events)
        self._lock = Lock()
        self._subscribers: list[asyncio.Queue] = []

    def push(self, event: FleetEvent) -> None:
        """Add an event and notify all subscribers."""
        with self._lock:
            self._events.append(event)
        # Notify async subscribers (safe to call from sync context)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # Drop if subscriber is slow

    def get(self, worker: str | None = None, since: str | None = None) -> list[FleetEvent]:
        """Get events, optionally filtered by worker and/or timestamp."""
        with self._lock:
            events = list(self._events)
        if worker:
            events = [e for e in events if e.worker_name == worker]
        if since:
            events = [e for e in events if e.timestamp > since]
        return events

    def subscribe(self) -> asyncio.Queue:
        """Create a new subscriber queue for SSE streaming."""
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        self._subscribers = [s for s in self._subscribers if s is not q]

# TODO: add persistence layer for events across restarts
