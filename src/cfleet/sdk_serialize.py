"""Serialize Claude Code SDK messages and content blocks to plain dicts.

Used by `cfleet agent` to forward SDK events over the WebSocket as JSON.
Kept separate from the heavy SDK imports in cli.py so the conversion logic
stays trivially unit-testable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def serialize_content_block(block: Any) -> dict:
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


def serialize_message(msg: Any) -> dict:
    """Convert an SDK message to a serializable dict."""
    msg_type = type(msg).__name__
    d: dict[str, Any] = {
        "type": msg_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if msg_type == "AssistantMessage":
        d["role"] = "assistant"
        d["content"] = [serialize_content_block(b) for b in msg.content]
        d["model"] = msg.model
    elif msg_type == "UserMessage":
        d["role"] = "user"
        if isinstance(msg.content, str):
            d["content"] = [{"type": "TextBlock", "text": msg.content}]
        else:
            d["content"] = [serialize_content_block(b) for b in msg.content]
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
