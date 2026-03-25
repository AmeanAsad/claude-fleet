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
