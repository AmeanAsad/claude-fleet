"""Worker relay — lightweight FastAPI service wrapping the Claude Code SDK.

Runs on each fleet worker (VM or container). The fleet server communicates
with it over SSH tunnels (cloud) or Docker networking (containers).

Start with:
    python3 worker_relay.py --port 8421
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class RelayState:
    """Mutable state for the relay process."""

    def __init__(self) -> None:
        self.status: str = "idle"  # idle | working | error
        self.session_id: str | None = None
        self.messages: list[dict] = []
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_cache_read_tokens: int = 0
        self.total_cache_creation_tokens: int = 0
        self.total_cost_usd: float = 0.0
        self.current_prompt: str | None = None
        self.error: str | None = None
        self._task: asyncio.Task | None = None
        self._subscribers: list[asyncio.Queue] = []

    def add_message(self, msg: dict) -> None:
        self.messages.append(msg)
        for q in self._subscribers:
            q.put_nowait(msg)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers = [s for s in self._subscribers if s is not q]


state = RelayState()


# ---------------------------------------------------------------------------
# SDK interaction
# ---------------------------------------------------------------------------

def _serialize_content_block(block: Any) -> dict:
    """Convert an SDK content block to a serializable dict."""
    block_type = type(block).__name__
    d: dict[str, Any] = {"type": block_type}

    if block_type == "TextBlock":
        d["text"] = block.text
    elif block_type == "ThinkingBlock":
        d["thinking"] = block.thinking
    elif block_type == "ToolUseBlock":
        d["tool_name"] = block.name
        d["tool_input"] = block.input
        d["tool_id"] = block.id
    elif block_type == "ToolResultBlock":
        d["tool_id"] = block.tool_use_id
        d["content"] = str(block.content) if block.content else ""
        d["is_error"] = bool(block.is_error)
    else:
        d["raw"] = str(block)

    return d


def _serialize_message(msg: Any) -> dict:
    """Convert an SDK message to a serializable dict."""
    msg_type = type(msg).__name__
    d: dict[str, Any] = {
        "type": msg_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if msg_type == "AssistantMessage":
        d["role"] = "assistant"
        d["content"] = [_serialize_content_block(b) for b in msg.content]
        d["model"] = msg.model
    elif msg_type == "UserMessage":
        d["role"] = "user"
        if isinstance(msg.content, str):
            d["content"] = [{"type": "TextBlock", "text": msg.content}]
        else:
            d["content"] = [_serialize_content_block(b) for b in msg.content]
    elif msg_type == "ResultMessage":
        d["role"] = "result"
        d["session_id"] = msg.session_id
        d["total_cost_usd"] = msg.total_cost_usd
        d["is_error"] = msg.is_error
        d["num_turns"] = msg.num_turns
        d["duration_ms"] = msg.duration_ms
        if msg.result:
            d["content"] = [{"type": "TextBlock", "text": msg.result}]
        else:
            d["content"] = []
        if msg.usage:
            d["usage"] = msg.usage
    elif msg_type == "SystemMessage":
        d["role"] = "system"
