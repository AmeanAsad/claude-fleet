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
