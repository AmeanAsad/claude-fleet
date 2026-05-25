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
# Output scrubbing — redact known secrets before sending to server
# ---------------------------------------------------------------------------

class SecretScrubber:
    """Replaces known secret values with [REDACTED] in outbound messages."""

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}

    def load_from_env_file(self, path: str = "") -> None:
        from pathlib import Path
        env_path = Path(path) if path else Path.home() / ".cfleet-env"
        if not env_path.exists():
            return
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 8:
                self._secrets[key.strip()] = value

    def add_secret(self, name: str, value: str) -> None:
        if len(value) >= 8:
            self._secrets[name] = value

    def scrub(self, text: str) -> str:
        for name, value in self._secrets.items():
            if value in text:
                text = text.replace(value, f"[REDACTED:{name}]")
        return text

    def scrub_dict(self, d: dict) -> dict:
        return json.loads(self.scrub(json.dumps(d)))


_scrubber = SecretScrubber()


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
        msgs = [_scrubber.scrub_dict(m) for m in state.messages[offset:offset + limit]]
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
                        yield {"event": "message", "data": json.dumps(_scrubber.scrub_dict(msg))}
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
# WebSocket client — connects to central server for command dispatch
# ---------------------------------------------------------------------------


async def _ws_client_loop(
    server_url: str,
    token: str,
    worker_name: str,
    machine_name: str,
    model: str,
    cwd: str,
) -> None:
    """Connect to the central server via WebSocket, receive commands, stream events."""
    import websockets

    ws_url = server_url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    backoff = 1.0
    max_backoff = 60.0

    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                # Register
                await ws.send(json.dumps({
                    "type": "register",
                    "token": token,
                    "worker_name": worker_name,
                    "machine_name": machine_name,
                }))

                reg_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if reg_response.get("type") != "registered":
                    print(f"[ws] Registration failed: {reg_response}")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)
                    continue

                print(f"[ws] Connected to server as {worker_name}")
                backoff = 1.0

                # Forward relay events to server
                event_queue = state.subscribe()

                async def _forward_events():
                    try:
                        while True:
                            event = await event_queue.get()
                            await ws.send(json.dumps({
                                "type": "event",
                                "data": _scrubber.scrub_dict(event),
                            }))
                    except Exception:
                        pass
                    finally:
                        state.unsubscribe(event_queue)

                forward_task = asyncio.create_task(_forward_events())

                # Heartbeat task
                async def _heartbeat():
                    while True:
                        await asyncio.sleep(30)
                        try:
                            await ws.send(json.dumps({"type": "heartbeat"}))
                        except Exception:
                            break

                heartbeat_task = asyncio.create_task(_heartbeat())

                # Listen for commands
                try:
                    async for raw in ws:
                        data = json.loads(raw)
                        cmd_type = data.get("type")
                        request_id = data.get("request_id", "")

                        if cmd_type == "ask":
                            prompt = data.get("prompt", "")
                            if state.status == "working":
                                await ws.send(json.dumps({
                                    "type": "response",
                                    "request_id": request_id,
                                    "data": {"error": "Agent is already working"},
                                }))
                            else:
                                state._task = asyncio.create_task(_run_agent(prompt, model, cwd))
                                await ws.send(json.dumps({
                                    "type": "response",
                                    "request_id": request_id,
                                    "data": {"ok": True, "status": "working"},
                                }))
                                await ws.send(json.dumps({
                                    "type": "status_update",
                                    "status": "working",
                                }))

                                async def _notify_completion(task, _ws=ws):
                                    try:
                                        await task
                                    except Exception:
                                        pass
                                    try:
                                        await _ws.send(json.dumps({
                                            "type": "status_update",
                                            "status": state.status,
                                        }))
                                    except Exception:
                                        pass

                                asyncio.create_task(_notify_completion(state._task))

                        elif cmd_type == "interrupt":
                            if state._task and not state._task.done():
                                state._task.cancel()
                                await ws.send(json.dumps({
                                    "type": "response",
                                    "request_id": request_id,
                                    "data": {"ok": True, "status": "interrupted"},
                                }))
                            else:
                                await ws.send(json.dumps({
                                    "type": "response",
                                    "request_id": request_id,
                                    "data": {"ok": True, "status": "not_running"},
                                }))

                        elif cmd_type == "status":
                            await ws.send(json.dumps({
                                "type": "response",
                                "request_id": request_id,
                                "data": {
                                    "status": state.status,
                                    "session_id": state.session_id,
                                    "message_count": len(state.messages),
                                    "total_input_tokens": state.total_input_tokens,
                                    "total_output_tokens": state.total_output_tokens,
                                    "total_cost_usd": state.total_cost_usd,
                                },
                            }))

                        elif cmd_type == "messages":
                            offset = data.get("offset", 0)
                            limit = data.get("limit", 200)
                            msgs = [_scrubber.scrub_dict(m) for m in state.messages[offset:offset + limit]]
                            await ws.send(json.dumps({
                                "type": "response",
                                "request_id": request_id,
                                "data": {
                                    "messages": msgs,
                                    "total": len(state.messages),
                                    "offset": offset,
                                },
                            }))

                        elif cmd_type == "heartbeat_ack":
                            pass

                finally:
                    forward_task.cancel()
                    heartbeat_task.cancel()

        except Exception as e:
            print(f"[ws] Connection error: {e}, reconnecting in {backoff:.0f}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


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
    parser.add_argument("--server-url", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--worker-name", default="")
    parser.add_argument("--machine-name", default="")
    args = parser.parse_args()

    server_url = args.server_url or os.environ.get("CFLEET_SERVER_URL", "")
    token = args.token or os.environ.get("CFLEET_TOKEN", "")
    worker_name = args.worker_name or os.environ.get("CFLEET_WORKER_NAME", "")
    machine_name = args.machine_name or os.environ.get("CFLEET_MACHINE_NAME", "")

    _scrubber.load_from_env_file()
    if token:
        _scrubber.add_secret("CFLEET_TOKEN", token)
    for env_key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_API_KEY"):
        val = os.environ.get(env_key, "")
        if val:
            _scrubber.add_secret(env_key, val)

    effective_model = args.model or os.environ.get("CFLEET_MODEL", "claude-opus-4-6")
    app = create_relay_app(model=effective_model, cwd=args.cwd)

    if server_url and worker_name:
        # Run both the local HTTP server and the WebSocket client
        async def _run_both():
            import uvicorn as _uv
            config = _uv.Config(app, host=args.host, port=args.port, log_level="warning")
            server = _uv.Server(config)

            ws_task = asyncio.create_task(
                _ws_client_loop(server_url, token, worker_name, machine_name, effective_model, args.cwd)
            )

            try:
                await server.serve()
            finally:
                ws_task.cancel()

        asyncio.run(_run_both())
    else:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
