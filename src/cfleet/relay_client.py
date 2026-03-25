"""HTTP client for communicating with the worker relay service.

Used by FleetEngine, the API server, and the TUI to interact with workers
through the Agent SDK relay instead of tmux.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx


class RelayClient:
    """Communicates with the relay service running on a fleet worker.

    For cloud VMs: base_url points to an SSH-tunneled local port.
    For Docker containers: base_url points to the container's network IP.
    """

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health_check(self) -> bool:
        """Check if the relay is running. Synchronous for simple polling."""
        try:
            with httpx.Client(timeout=5.0) as client:
                r = client.get(f"{self.base_url}/health")
                return r.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException, OSError):
            return False

    def get_status_sync(self) -> dict:
        """Get relay status. Synchronous for CLI/engine use."""
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(f"{self.base_url}/status")
            r.raise_for_status()
            return r.json()

    def send_prompt_sync(self, prompt: str) -> dict:
        """Send a prompt to the worker. Synchronous."""
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(f"{self.base_url}/prompt", json={"prompt": prompt})
            r.raise_for_status()
            return r.json()

    def interrupt_sync(self) -> dict:
        """Interrupt the current agent run. Synchronous."""
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(f"{self.base_url}/interrupt")
            r.raise_for_status()
            return r.json()

    def get_messages_sync(self, offset: int = 0, limit: int = 200) -> dict:
        """Get conversation history. Synchronous."""
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(
                f"{self.base_url}/messages",
                params={"offset": offset, "limit": limit},
            )
            r.raise_for_status()
            return r.json()

    def is_idle(self) -> bool:
        """Check if the worker is idle."""
        try:
            status = self.get_status_sync()
            return status.get("status") == "idle"
        except (httpx.ConnectError, httpx.TimeoutException, OSError):
            return False

    def is_alive(self) -> bool:
        """Check if the relay process is reachable."""
        return self.health_check()

    # ------------------------------------------------------------------
    # Async methods (for API server and TUI)
    # ------------------------------------------------------------------

    async def send_prompt(self, prompt: str) -> dict:
        """Send a prompt to the worker. Async."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.base_url}/prompt", json={"prompt": prompt})
            r.raise_for_status()
            return r.json()

    async def get_status(self) -> dict:
        """Get relay status. Async."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base_url}/status")
            r.raise_for_status()
            return r.json()

    async def get_messages(self, offset: int = 0, limit: int = 200) -> dict:
        """Get conversation history. Async."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{self.base_url}/messages",
                params={"offset": offset, "limit": limit},
            )
            r.raise_for_status()
            return r.json()

    async def interrupt(self) -> dict:
        """Interrupt the current agent run. Async."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.base_url}/interrupt")
            r.raise_for_status()
            return r.json()

    async def stream_messages(self) -> AsyncIterator[dict]:
        """Stream new messages via SSE. Yields parsed message dicts."""
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET",
                f"{self.base_url}/stream",
                headers={"Accept": "text/event-stream"},
            ) as response:
                buffer = ""
                event_type = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.rstrip("\r")
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data = line[5:].strip()
                            if event_type == "message" and data:
                                try:
                                    yield json.loads(data)
                                except json.JSONDecodeError:
                                    pass
                            event_type = ""
                        elif line == "":
                            event_type = ""

    # ------------------------------------------------------------------
    # Sync streaming (for CLI `logs -f`)
    # ------------------------------------------------------------------

    def stream_messages_sync(self) -> Iterator[dict]:
        """Stream new messages via SSE. Synchronous iterator for CLI use."""
        with httpx.Client(timeout=None) as client:
            with client.stream(
                "GET",
                f"{self.base_url}/stream",
                headers={"Accept": "text/event-stream"},
            ) as response:
                buffer = ""
                event_type = ""
                for chunk in response.iter_text():
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.rstrip("\r")
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data = line[5:].strip()
                            if event_type == "message" and data:
                                try:
                                    yield json.loads(data)
                                except json.JSONDecodeError:
                                    pass
                                event_type = ""
                        elif line == "":
                            event_type = ""


def format_message(msg: dict) -> str:
    """Format a structured message for terminal display.

    Used by CLI `logs` and TUI log panel.
    """
    role = msg.get("role", "unknown")
    msg_type = msg.get("type", "")

    # Skip noisy system init messages
    if msg_type == "SystemMessage" and msg.get("subtype") == "init":
        return ""

    content_blocks = msg.get("content", [])
    parts: list[str] = []

    for block in content_blocks:
        block_type = block.get("type", "")
        if block_type == "TextBlock":
            text = block.get("text", "")
            if role == "user":
                parts.append(f"[bold cyan]You:[/bold cyan] {text}")
            elif role == "assistant":
                parts.append(text)
            elif role == "system":
                parts.append(f"[dim]{text}[/dim]")
            elif role == "result":
