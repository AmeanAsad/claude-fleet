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
        d["subtype"] = msg.subtype
        d["content"] = [{"type": "TextBlock", "text": json.dumps(msg.data)}]
    else:
        d["role"] = "unknown"
        d["raw"] = str(msg)

    return d


async def _run_agent(prompt: str, model: str, cwd: str) -> None:
    """Run the Agent SDK query loop and store messages."""
    from claude_code_sdk import query, ClaudeCodeOptions

    state.status = "working"
    state.current_prompt = prompt
    state.error = None

    # Record the user prompt as a message
    user_msg = {
        "type": "UserPrompt",
        "role": "user",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "content": [{"type": "TextBlock", "text": prompt}],
    }
    state.add_message(user_msg)

    kwargs = dict(
        model=model,
        cwd=cwd,
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Write", "Edit", "MultiEdit", "Bash", "Glob", "Grep", "WebFetch"],
    )
    if state.session_id:
        kwargs["resume"] = state.session_id
    options = ClaudeCodeOptions(**kwargs)

    try:
        async for message in query(prompt=prompt, options=options):
            serialized = _serialize_message(message)
            state.add_message(serialized)

            # Track session ID from ResultMessage
            msg_type = type(message).__name__
            if msg_type == "ResultMessage":
                state.session_id = message.session_id

                # Track token usage
                if message.usage:
                    state.total_input_tokens += message.usage.get("input_tokens", 0)
                    state.total_output_tokens += message.usage.get("output_tokens", 0)
                    state.total_cache_read_tokens += message.usage.get("cache_read_input_tokens", 0)
                    state.total_cache_creation_tokens += message.usage.get("cache_creation_input_tokens", 0)
                if message.total_cost_usd:
                    state.total_cost_usd = message.total_cost_usd

        state.status = "idle"
    except asyncio.CancelledError:
        state.status = "idle"
        state.add_message({
            "type": "SystemEvent",
            "role": "system",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "content": [{"type": "TextBlock", "text": "Agent interrupted."}],
        })
    except Exception as e:
        state.status = "error"
        state.error = str(e)
        state.add_message({
            "type": "SystemEvent",
            "role": "system",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "content": [{"type": "TextBlock", "text": f"Error: {e}"}],
        })


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

class PromptRequest(BaseModel):
    prompt: str


def create_relay_app(model: str = "", cwd: str = "/workspace") -> FastAPI:
    effective_model = model or os.environ.get("CFLEET_MODEL", "claude-opus-4-6")

    app = FastAPI(title="cfleet-worker-relay", version="0.1.0")

    @app.get("/health")
    async def health():
        return {"ok": True, "status": state.status}

    @app.get("/status")
    async def get_status():
        return {
            "status": state.status,
            "session_id": state.session_id,
            "current_prompt": state.current_prompt,
            "error": state.error,
            "message_count": len(state.messages),
            "total_input_tokens": state.total_input_tokens,
            "total_output_tokens": state.total_output_tokens,
            "total_cache_read_tokens": state.total_cache_read_tokens,
            "total_cache_creation_tokens": state.total_cache_creation_tokens,
            "total_cost_usd": state.total_cost_usd,
        }

    @app.post("/prompt")
    async def send_prompt(req: PromptRequest):
        if state.status == "working":
            raise HTTPException(status_code=409, detail="Agent is already working. Interrupt first.")
        state._task = asyncio.create_task(_run_agent(req.prompt, effective_model, cwd))
        return {"ok": True, "status": "working"}

    @app.post("/interrupt")
    async def interrupt():
        if state._task and not state._task.done():
            state._task.cancel()
            return {"ok": True, "status": "interrupted"}
        return {"ok": True, "status": "not_running"}

    @app.get("/messages")
    async def get_messages(offset: int = 0, limit: int = 200):
        msgs = state.messages[offset:offset + limit]
        return {
            "messages": msgs,
            "total": len(state.messages),
            "offset": offset,
        }

    @app.get("/stream")
    async def stream_messages():
        """SSE stream of new messages as they arrive."""
        queue = state.subscribe()

        async def event_generator():
            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=30.0)
                        yield {"event": "message", "data": json.dumps(msg)}
                    except asyncio.TimeoutError:
                        yield {
                            "event": "keepalive",
                            "data": json.dumps({"status": state.status}),
                        }
            finally:
                state.unsubscribe(queue)

        return EventSourceResponse(event_generator())

    return app


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="cfleet worker relay")
    parser.add_argument("--port", type=int, default=8421)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model", default="")
    parser.add_argument("--cwd", default="/workspace")
    args = parser.parse_args()

    app = create_relay_app(model=args.model, cwd=args.cwd)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
