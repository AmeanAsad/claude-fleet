"""Prime-agent backend — drives a resident prime-agent daemon session as a fleet worker.

This module is the only place that knows how fleet workers map onto prime-agent
concepts. Everything goes through prime-agent's public CLI (`prime-agent send /
list / stop / rename / schedule`) or the documented session JSONL format — no
hand-rolled session state, no internal daemon-socket protocol.

Lifecycle mapping (validated against prime-agent 0.7.2):

  spawn   = create a named resident session (RPC create + heartbeat promotion)
  ask     = `prime-agent send <worker> <prompt>`  (auto-wakes saved sessions)
  status  = `prime-agent list --json` (lifecycle / activity / messageCount)
  history = parse `~/.prime/agent/sessions/<uuid>.jsonl` (v3 tree format)
  kill    = `prime-agent stop <worker>` (session archived, stays resumable)
  restart = relay re-adopts by worker name; first `send` re-wakes the session
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

PRIME_BIN = shutil.which("prime-agent") or "prime-agent"
MIN_PRIME_VERSION = (0, 7, 0)  # send-wakes-saved + RPC heartbeat promotion (validated on 0.7.0 + 0.7.2)

# Session name used inside prime-agent == fleet worker name.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class PrimeBackendError(RuntimeError):
    """Raised when a prime-agent CLI interaction fails."""


def _clean_prime_env() -> dict:
    """Environment for prime-agent subprocess calls, with any ambient session
    identity stripped.

    When the relay runs under a prime-agent session itself (e.g. an operator's
    interactive prime-agent, or tests), PRIME_AGENT_*/RLM_* env vars leak the
    *caller's* session identity into every CLI invocation. The daemon then
    treats fleet `send`s as coming from that session's family — and workers
    answering "reply to the sender" route agent-messages to the operator's
    personal session. Fleet calls must be identity-neutral.
    """
    return {
        k: v for k, v in os.environ.items()
        if not (k.startswith("PRIME_AGENT_") or k.startswith("RLM_"))
    }


# ---------------------------------------------------------------------------
# subprocess helpers
# ---------------------------------------------------------------------------

def _run_prime(args: list[str], timeout: float = 30.0, input_text: str | None = None) -> subprocess.CompletedProcess:
    """Run a prime-agent CLI command. Never raises on non-zero; caller checks."""
    try:
        return subprocess.run(
            [PRIME_BIN, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            env=_clean_prime_env(),
        )
    except subprocess.TimeoutExpired as e:
        raise PrimeBackendError(f"prime-agent {' '.join(args[:2])} timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise PrimeBackendError("prime-agent CLI not found on PATH") from e


def prime_version() -> tuple[int, int, int] | None:
    """Best-effort installed prime-agent version, e.g. (0, 7, 2). None if unknown."""
    try:
        cp = _run_prime(["--version"], timeout=10)
    except PrimeBackendError:
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", (cp.stdout or "") + (cp.stderr or ""))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def check_prime_available() -> str | None:
    """Return a human-readable problem string, or None if prime-agent looks usable."""
    if not shutil.which("prime-agent"):
        return "prime-agent CLI not found on PATH (install: https://primeintellect.ai prime-agent)"
    v = prime_version()
    if v is None:
        return "could not determine prime-agent version"
    if v < MIN_PRIME_VERSION:
        return f"prime-agent {v[0]}.{v[1]}.{v[2]} is too old; need >= {'.'.join(map(str, MIN_PRIME_VERSION))}"
    return None


# ---------------------------------------------------------------------------
# daemon queries
# ---------------------------------------------------------------------------

def list_sessions(include_saved: bool = True) -> list[dict]:
    """All daemon-known sessions (live + saved/draft/archived when include_saved)."""
    args = ["list", "--json"]
    if include_saved:
        args.insert(1, "--all")
    cp = _run_prime(args, timeout=20)
    if cp.returncode != 0:
        raise PrimeBackendError(f"prime-agent list failed: {cp.stderr.strip() or cp.stdout.strip()}")
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        raise PrimeBackendError(f"prime-agent list returned non-JSON: {cp.stdout[:200]}") from e
    return data.get("sessions", [])


def find_session_by_name(worker_name: str) -> dict | None:
    """Find a top-level prime session whose display name is exactly `worker_name`.

    Only top-level sessions are eligible (RLM subagents with colliding names are
    never adopted as fleet workers).
    """
    matches = []
    for s in list_sessions(include_saved=True):
        if s.get("sessionName") != worker_name:
            continue
        rk = s.get("runtimeKind")
        if rk and rk not in ("top-level",):
            continue
        matches.append(s)
    if not matches:
        return None
    # Prefer a live/draft session over a saved one.
    matches.sort(key=lambda s: 0 if s.get("lifecycle") in ("live", "draft") else 1)
    return matches[0]


# ---------------------------------------------------------------------------
# session info
# ---------------------------------------------------------------------------

@dataclass
class PrimeSessionInfo:
    """What the relay needs to talk about one prime session."""

    worker_name: str
    session_id: str = ""          # stable session UUID (survives stop/resume)
    session_file: str = ""        # ~/.prime/agent/sessions/<uuid>.jsonl
    active_id: str = ""           # daemon activeSessionId (changes across wake/sleep)
    lifecycle: str = ""           # live | draft | saved | archived (as reported)
    activity: str = ""            # idle | working (live sessions only)
    message_count: int = 0
    model: str = ""               # resolved model id (e.g. kimi-k3)

    @classmethod
    def from_list_entry(cls, worker_name: str, entry: dict) -> "PrimeSessionInfo":
        return cls(
            worker_name=worker_name,
            session_id=entry.get("sessionId", "") or "",
            session_file=entry.get("sessionFile", "") or "",
            active_id=entry.get("id", "") or entry.get("activeSessionId", "") or "",
            lifecycle=entry.get("lifecycle", "") or "",
            activity=entry.get("activity", "") or "",
            message_count=int(entry.get("messageCount") or 0),
        )


# ---------------------------------------------------------------------------
# the backend
# ---------------------------------------------------------------------------

class PrimeAgentBackend:
    """Fleet-worker-facing handle over a named resident prime-agent session."""

    def __init__(self, worker_name: str, cwd: str, model: str | None = None):
        if not _NAME_RE.match(worker_name):
            raise PrimeBackendError(f"invalid worker name for prime session: {worker_name!r}")
        self.worker_name = worker_name
        self.cwd = cwd
        self.model = model or ""

    # -- creation ---------------------------------------------------------

    def _rpc_create_session(self, resume_session_id: str | None = None) -> PrimeSessionInfo:
        """Create (or resume) a session and promote it to a resident daemon worker.

        Recipe (validated on 0.7.2):
          1. `prime-agent --mode rpc [--resume <id>] --cwd <dir> [--model <m>]`
          2. RPC `set_heartbeat` → daemon promotes the client-owned session to
             resident and returns activeSessionId + sessionId + sessionFile.
          3. Close RPC stdin (session stays resident).
          4. `prime-agent rename <activeId> <worker_name>`.
          5. `prime-agent schedule cancel <heartbeat job id>` (the heartbeat was
             only the promotion vehicle).
        """
        args = ["--mode", "rpc", "--cwd", self.cwd]
        if resume_session_id:
            args += ["--resume", resume_session_id]
        if self.model:
            args += ["--model", self.model]

        proc = subprocess.Popen(
            [PRIME_BIN, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=self.cwd or None,
            bufsize=1,
            env=_clean_prime_env(),
        )
        info: dict[str, str] = {}
        try:
            cmd = {
                "id": "fleet-promote",
                "type": "set_heartbeat",
                "schedule": "every 30m",
                "prompt": "fleet residency placeholder (cancelled immediately)",
            }
            assert proc.stdin and proc.stdout
            proc.stdin.write(json.dumps(cmd) + "\n")
            proc.stdin.flush()
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") != "fleet-promote":
                    continue
                if not msg.get("success"):
                    raise PrimeBackendError(f"heartbeat promotion rejected: {line[:300]}")
                hb = (msg.get("data") or {}).get("heartbeat") or {}
                info = {
                    "job_id": hb.get("id", ""),
                    "active_id": hb.get("activeSessionId", ""),
                    "session_id": hb.get("sessionId", ""),
                    "session_file": hb.get("sessionFile", ""),
                }
                break
            # Grab the resolved model so the fleet UI can display it even
            # before the first turn.
            if info.get("session_id"):
                try:
                    proc.stdin.write(json.dumps({"id": "fleet-model", "type": "get_state"}) + "\n")
                    proc.stdin.flush()
                    mdeadline = time.monotonic() + 10
                    while time.monotonic() < mdeadline:
                        line = proc.stdout.readline()
                        if not line:
                            break
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if msg.get("id") != "fleet-model" or not msg.get("success"):
                            continue
                        model_obj = (msg.get("data") or {}).get("model") or {}
                        if isinstance(model_obj, dict) and model_obj.get("id"):
                            info["model"] = model_obj["id"]
                        break
                except Exception:
                    pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
            except Exception:
                pass

        if not info.get("session_id"):
            raise PrimeBackendError(
                "prime-agent RPC promotion returned no session (is the daemon healthy? "
                "try `prime-agent status`)"
            )

        # Rename to the worker name (best-effort; adoption still works by id).
        selector = info["active_id"] or info["session_id"]
        cp = _run_prime(["rename", selector, self.worker_name], timeout=20)
        if cp.returncode != 0:
            raise PrimeBackendError(
                f"prime-agent rename failed: {cp.stderr.strip() or cp.stdout.strip()}"
            )

        # Cancel the placeholder heartbeat — residency survives cancellation.
        if info.get("job_id"):
            _run_prime(["schedule", "cancel", info["job_id"]], timeout=20)

        return PrimeSessionInfo(
            worker_name=self.worker_name,
            session_id=info["session_id"],
            session_file=info["session_file"],
            active_id=info["active_id"],
            lifecycle="live",
            model=info.get("model", ""),
        )

    # -- adoption / creation entry point -----------------------------------

    def ensure_session(self, resume_session_id: str | None = None) -> PrimeSessionInfo:
        """Idempotently get this worker's prime session.

        Order: adopt a live/draft session with our name → adopt a saved one (it
        wakes on first send) → RPC-resume the marker's session id → create fresh.
        """
        existing = find_session_by_name(self.worker_name)
        if existing is not None:
            return PrimeSessionInfo.from_list_entry(self.worker_name, existing)

        if resume_session_id:
            try:
                return self._rpc_create_session(resume_session_id=resume_session_id)
            except PrimeBackendError:
                # Fall through to a fresh session — a lost session id must not
                # brick the worker.
                pass

        return self._rpc_create_session()

    # -- prompts ------------------------------------------------------------

    def send(self, prompt: str, timeout: float = 30.0) -> None:
        """Deliver a prompt to the worker's session. Wakes saved sessions."""
        cp = _run_prime(
            ["send", self.worker_name, prompt, "--json"],
            timeout=timeout,
        )
        out = (cp.stdout or "") + (cp.stderr or "")
        # Note: prime-agent's exit code is unreliable across failure modes
        # (0.7.2 returns 0 for some "Error: ..." outcomes), so sniff the output.
        ok = cp.returncode == 0 and "Error:" not in out and (
            '"deliveryStatus"' in out or '"target"' in out or "Sent to" in out
        )
        if not ok:
            raise PrimeBackendError(f"prime-agent send failed: {out.strip()[:300]}")

    def wake(self) -> PrimeSessionInfo:
        """Ensure the session is live (resident) WITHOUT injecting a prompt.

        Live/draft sessions are returned as-is. Saved/archived ones are resumed
        through the RPC promotion path (which preserves name + full history).
        A missing session is created fresh.
        """
        entry = find_session_by_name(self.worker_name)
        if entry is not None and entry.get("lifecycle") in ("live", "draft"):
            return PrimeSessionInfo.from_list_entry(self.worker_name, entry)
        resume_id = (entry or {}).get("sessionId") or None
        return self._rpc_create_session(resume_session_id=resume_id)

    def stop(self) -> None:
        """Stop the resident worker (session stays saved + resumable).

        Idempotent: an already-stopped or unknown session is a no-op.
        """
        cp = _run_prime(["stop", self.worker_name, "--json"], timeout=30)
        out = (cp.stdout or "") + (cp.stderr or "")
        if cp.returncode != 0:
            benign = ("unknown" in out.lower() or "not found" in out.lower()
                      or "no active" in out.lower() or '"success":true' in out.lower())
            if not benign:
                raise PrimeBackendError(f"prime-agent stop failed: {out.strip()[:300]}")

    def purge_files(self, session_id: str | None = None) -> bool:
        """Delete the session JSONL and artifact dir from disk. Best-effort.

        Returns True if anything was removed.
        """
        removed = False
        sid = session_id or ""
        session_file = ""
        entry = find_session_by_name(self.worker_name)
        if entry is not None:
            session_file = entry.get("sessionFile", "") or ""
            sid = sid or entry.get("sessionId", "") or ""

        candidates = []
        if session_file:
            candidates.append(Path(session_file))
        if sid:
            candidates.append(Path.home() / ".prime" / "agent" / "sessions" / f"{sid}.jsonl")
        for p in candidates:
            try:
                if p.exists():
                    p.unlink()
                    removed = True
            except OSError:
                pass
        if sid:
            artifacts = Path.home() / ".prime" / "agent" / "session-artifacts" / sid
            if artifacts.exists():
                shutil.rmtree(artifacts, ignore_errors=True)
                removed = True
        return removed

    # -- status -------------------------------------------------------------

    def status(self) -> dict:
        """Fleet-shaped status for this worker, from the daemon's point of view."""
        entry = find_session_by_name(self.worker_name)
        if entry is None:
            return {
                "status": "stopped",
                "activity": "",
                "lifecycle": "missing",
                "message_count": 0,
                "session_id": "",
                "session_file": "",
                "model": "",
            }
        info = PrimeSessionInfo.from_list_entry(self.worker_name, entry)
        lifecycle = info.lifecycle or "live"
        if lifecycle in ("saved", "archived"):
            fleet_status = "idle"      # resumable; wakes on next send
        elif lifecycle == "draft":
            fleet_status = "idle"
        elif info.activity == "working":
            fleet_status = "working"
        else:
            fleet_status = "idle"
        return {
            "status": fleet_status,
            "activity": info.activity,
            "lifecycle": lifecycle,
            "message_count": info.message_count,
            "session_id": info.session_id,
            "session_file": info.session_file,
            "model": (entry.get("model") or {}).get("id", "") if isinstance(entry.get("model"), dict) else str(entry.get("model") or ""),
        }


# ---------------------------------------------------------------------------
# prime session JSONL → dashboard Message conversion
# ---------------------------------------------------------------------------

def _text_of(content: Any) -> str:
    """Join the text blocks of a prime message content array."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
    )


def prime_entry_to_message(entry: dict) -> Optional[dict]:
    """Convert one prime-agent session-JSONL entry to the fleet dashboard Message
    shape (the same shape `sdk_serialize` / `_jsonl_entry_to_message` produce for
    claude workers). Returns None for entries that should not be displayed.
    """
    etype = entry.get("type")
    ts = entry.get("timestamp", "")

    # Prompts delivered via `prime-agent send` arrive as agent-to-agent custom
    # messages; the clean operator text is under details.message.
    if etype == "custom_message":
        if entry.get("customType") != "agent_message":
            return None
        details = entry.get("details") or {}
        text = details.get("message")
        if not text:
            raw = entry.get("content", "")
            # Strip the "Agent-to-agent message received.\n...\n\n" envelope.
            parts = raw.split("\n\n", 1)
            text = parts[1] if len(parts) == 2 else raw
        if not text:
            return None
        return {
            "type": "UserPrompt",
            "role": "user",
            "timestamp": ts,
            "content": [{"type": "TextBlock", "text": text}],
        }

    if etype != "message":
        return None

    msg = entry.get("message") or {}
    role = msg.get("role", "")
    content = msg.get("content")

    if role == "user":
        text = _text_of(content)
        if not text:
            return None
        return {
            "type": "UserPrompt",
            "role": "user",
            "timestamp": ts,
            "content": [{"type": "TextBlock", "text": text}],
        }

    if role == "assistant":
        blocks: list[dict] = []
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "thinking":
                    thinking = b.get("thinking", "")
                    if thinking:
                        blocks.append({"type": "ThinkingBlock", "thinking": thinking})
                elif bt == "text":
                    text = b.get("text", "")
                    if text:
                        blocks.append({"type": "TextBlock", "text": text})
                elif bt == "toolCall":
                    blocks.append({
                        "type": "ToolUseBlock",
                        "tool_name": b.get("name", ""),
                        "tool_input": b.get("arguments", {}),
                        "tool_id": b.get("id", ""),
                    })
        if not blocks:
            return None
        out = {
            "type": "AssistantMessage",
            "role": "assistant",
            "timestamp": ts,
            "content": blocks,
            "model": msg.get("model", ""),
        }
        usage = msg.get("usage")
        if isinstance(usage, dict) and usage:
            out["usage"] = {
                "input_tokens": usage.get("input") or usage.get("inputTokens") or usage.get("input_tokens") or 0,
                "output_tokens": usage.get("output") or usage.get("outputTokens") or usage.get("output_tokens") or 0,
            }
            cost = usage.get("cost")
            if isinstance(cost, dict):
                out["total_cost_usd"] = cost.get("total", 0) or 0
            elif isinstance(cost, (int, float)):
                out["total_cost_usd"] = cost
        return out

    if role == "toolResult":
        # Mirror the claude shape: tool results render as ToolResultBlock inside
        # a user-role message.
        text = _text_of(content)
        return {
            "type": "UserPrompt",
            "role": "user",
            "timestamp": ts,
            "content": [{
                "type": "ToolResultBlock",
                "content": text,
                "is_error": bool(msg.get("isError")),
                "tool_name": msg.get("toolName", ""),
            }],
        }

    return None


# ---------------------------------------------------------------------------
# paginated JSONL reader (mirrors cli._read_session_messages' contract)
# ---------------------------------------------------------------------------

def _iter_lines_reverse(path: Path, chunk_size: int = 65536) -> Iterator[bytes]:
    """Yield raw lines from `path` in reverse without loading the whole file."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        remaining = f.tell()
        buffer = b""
        while remaining > 0:
            read = min(chunk_size, remaining)
            remaining -= read
            f.seek(remaining)
            chunk = f.read(read)
            buffer = chunk + buffer
            lines = buffer.split(b"\n")
            if remaining > 0:
                buffer = lines[0]
                lines = lines[1:]
            else:
                buffer = b""
            for line in reversed(lines):
                if line.strip():
                    yield line


def read_prime_messages(
    jsonl_path: str | Path,
    *,
    before: int | None = None,
    limit: int = 200,
) -> dict:
    """Tail-first paginated read of a prime session JSONL.

    Same return contract as the claude reader: {messages, total, head, has_more}.
    """
    p = Path(jsonl_path)
    if not p.exists():
        return {"messages": [], "total": 0, "head": 0, "has_more": False}

    if before is None:
        collected: list[dict] = []
        total_before = 0
        filled = False
        for raw in _iter_lines_reverse(p):
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg = prime_entry_to_message(entry)
            if msg is None:
                continue
            if not filled:
                collected.append(msg)
                if len(collected) >= limit:
                    filled = True
            else:
                total_before += 1
        collected.reverse()
        total = total_before + len(collected)
        head = total_before
        return {"messages": collected, "total": total, "head": head, "has_more": head > 0}

    # Paginated older read. `before` is a message-index. Return the window
    # [max(0, before-limit) : before]. Walk forward, count displayable
    # messages, capture the slice, early-exit past `before`.
    start = max(0, before - limit)
    end = before
    collected: list[dict] = []
    count = 0
    with open(p, "rb") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = prime_entry_to_message(entry)
            if msg is None:
                continue
            if count >= end:
                break
            if count >= start:
                collected.append(msg)
            count += 1
    return {
        "messages": collected,
        "total": count,  # best-effort, may be low (mirrors the claude reader)
        "head": start,
        "has_more": start > 0,
    }


# ---------------------------------------------------------------------------
# worker relay loop — speaks the fleet worker WebSocket contract
# ---------------------------------------------------------------------------

class _PrimeRuntime:
    """Mutable per-relay state shared between the WS loop tasks."""

    def __init__(self, backend: "PrimeAgentBackend", info: PrimeSessionInfo):
        self.backend = backend
        self.info = info
        self.status: str = "idle"      # fleet status: idle | working | errored
        self.last_activity: str = ""   # last activity seen from the daemon
        self.tail_offset: int = 0      # byte offset into the session JSONL

    @property
    def jsonl_path(self) -> str:
        return self.info.session_file


async def _prime_jsonl_tailer(ws, runtime: _PrimeRuntime) -> None:
    """Push new session-JSONL entries to the server as dashboard events.

    Prime-agent persists each completed message as one JSONL entry, so a 0.5s
    poll gives near-live updates (same granularity as the claude TUI path).
    Starts at offset 0 on connect — the server/dashboard dedupe by content —
    and tracks a byte offset thereafter. Handles session-file replacement
    (resume creates a new generation) by re-reading from the start when the
    file shrinks or changes identity.
    """
    import asyncio
    import json
    from pathlib import Path

    while True:
        try:
            path = Path(runtime.jsonl_path) if runtime.jsonl_path else None
            if path is None or not path.exists():
                # Session file may not exist yet (fresh session, no prompts).
                refreshed = await asyncio.to_thread(find_session_by_name, runtime.backend.worker_name)
                if refreshed is not None:
                    runtime.info = PrimeSessionInfo.from_list_entry(runtime.backend.worker_name, refreshed)
                    runtime.tail_offset = 0
                await asyncio.sleep(1.0)
                continue

            size = path.stat().st_size
            if size < runtime.tail_offset:
                runtime.tail_offset = 0  # file replaced/truncated → rescan
            if size > runtime.tail_offset:
                with open(path, "r") as f:
                    f.seek(runtime.tail_offset)
                    chunk = f.read()
                runtime.tail_offset = size
                for line in chunk.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = prime_entry_to_message(entry)
                    if msg is None:
                        continue
                    try:
                        await ws.send(json.dumps({"type": "event", "data": msg}))
                    except Exception:
                        return
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(2.0)


async def _prime_status_poller(ws, runtime: _PrimeRuntime) -> None:
    """Mirror the daemon's activity (idle/working) into fleet status_updates."""
    import asyncio
    import json

    while True:
        try:
            st = await asyncio.to_thread(runtime.backend.status)
            activity = st.get("activity") or ""
            lifecycle = st.get("lifecycle") or ""
            if st.get("session_file") and st["session_file"] != runtime.info.session_file:
                runtime.info.session_file = st["session_file"]
                runtime.info.session_id = st.get("session_id", runtime.info.session_id)
                runtime.tail_offset = 0  # new generation → replay to resync dashboard

            if lifecycle in ("saved", "archived", "missing"):
                fleet_status = "idle"
            elif activity == "working":
                fleet_status = "working"
            else:
                fleet_status = "idle"

            if fleet_status != runtime.status:
                runtime.status = fleet_status
                try:
                    await ws.send(json.dumps({"type": "status_update", "status": fleet_status}))
                except Exception:
                    return
            runtime.last_activity = activity
            await asyncio.sleep(2.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(5.0)


async def _prime_handle_command(ws, data: dict, runtime: _PrimeRuntime, worker_name: str) -> None:
    """Handle server commands: ask, messages, status, interrupt."""
    import asyncio
    import json

    msg_type = data.get("type")
    request_id = data.get("request_id", "")

    if msg_type == "heartbeat_ack":
        return

    if msg_type == "ask":
        prompt = data.get("prompt", "")
        if not prompt:
            await ws.send(json.dumps({
                "type": "response", "request_id": request_id,
                "data": {"error": "Empty prompt"},
            }))
            return
        try:
            # Re-adopt in case the daemon evicted the idle worker since boot.
            await asyncio.to_thread(_ensure_prime_session, runtime)
            await asyncio.to_thread(runtime.backend.send, prompt)
        except PrimeBackendError as e:
            await ws.send(json.dumps({
                "type": "response", "request_id": request_id,
                "data": {"error": str(e)},
            }))
            return
        # If the session was busy, the daemon steers/queues the message instead
        # of rejecting it — a deliberate upgrade over the claude path, which
        # errors with "Agent is already working".
        busy_note = " (queued: session was busy)" if runtime.status == "working" else ""
        runtime.status = "working"
        await ws.send(json.dumps({
            "type": "response", "request_id": request_id,
            "data": {"ok": True, "status": "working", "note": busy_note.strip(" ()") if busy_note else ""},
        }))
        await ws.send(json.dumps({"type": "status_update", "status": "working"}))

    elif msg_type == "messages":
        limit = int(data.get("limit", 200))
        before = data.get("before")
        if before is None and "offset" in data and data.get("offset"):
            before = int(data["offset"]) + limit
        elif before is not None:
            before = int(before)
        result = await asyncio.to_thread(
            read_prime_messages, runtime.jsonl_path, before=before, limit=limit,
        )
        await ws.send(json.dumps({
            "type": "response", "request_id": request_id, "data": result,
        }))

    elif msg_type == "status":
        st = await asyncio.to_thread(runtime.backend.status)
        await ws.send(json.dumps({
            "type": "response",
            "request_id": request_id,
            "data": {
                "worker_name": worker_name,
                "backend": "prime",
                "model": st.get("model", ""),
                "session_id": st.get("session_id", ""),
                "status": runtime.status,
                "prime_lifecycle": st.get("lifecycle", ""),
                "prime_activity": st.get("activity", ""),
                "message_count": st.get("message_count", 0),
                "lock_owner": None,
            },
        }))

    elif msg_type == "interrupt":
        # prime-agent has no supported public API to abort another session's
        # in-flight turn (the daemon interrupt is internal protocol). Be honest.
        await ws.send(json.dumps({
            "type": "response",
            "request_id": request_id,
            "data": {
                "error": "interrupt is not supported for prime-agent workers yet "
                         "(prime-agent exposes no public per-session abort). "
                         "Use `cfleet kill` to tear down, or steer the session with "
                         "an `ask` describing the change."
            },
        }))


def _ensure_prime_session(runtime: _PrimeRuntime) -> None:
    """Sync helper: make sure the daemon knows our session; refresh the runtime."""
    info = runtime.backend.ensure_session(resume_session_id=runtime.info.session_id or None)
    if info.session_file != runtime.info.session_file:
        runtime.tail_offset = 0
    runtime.info = info


async def run_prime_worker(
    *,
    server_url: str,
    token: str,
    worker_name: str,
    machine_name: str,
    model: str,
    cwd: str,
    ssh_host: str = "",
    ssh_user: str = "",
    resume_session_id: str | None = None,
    info: PrimeSessionInfo | None = None,
) -> None:
    """Fleet worker relay backed by a resident prime-agent daemon session.

    Same WebSocket contract as the claude relay (`_agent_ws_loop`): register,
    heartbeat, event stream, and the ask/messages/status/interrupt commands.
    """
    import asyncio
    import json
    import websockets

    backend = PrimeAgentBackend(worker_name, cwd, model or None)

    if info is None:
        # Resolve/create the prime session BEFORE registering so the dashboard
        # can immediately read history. Runs in a thread: promotion takes seconds.
        info = await asyncio.to_thread(backend.ensure_session, resume_session_id)
    print(f"[cfleet agent] prime session {info.session_id} ({info.lifecycle}) at {info.session_file}")

    runtime = _PrimeRuntime(backend, info)

    ws_url = server_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    backoff = 1.0

    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                await ws.send(json.dumps({
                    "type": "register",
                    "token": token,
                    "worker_name": worker_name,
                    "machine_name": machine_name,
                    "session_id": runtime.info.session_id,
                    "cwd": cwd,
                    "model": model,
                    "ssh_host": ssh_host,
                    "ssh_user": ssh_user,
                    "skip_permissions": True,
                    "agent_backend": "prime",
                }))

                reg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if reg.get("type") != "registered":
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue

                backoff = 1.0
                tasks = [
                    asyncio.create_task(_prime_heartbeat(ws)),
                    asyncio.create_task(_prime_jsonl_tailer(ws, runtime)),
                    asyncio.create_task(_prime_status_poller(ws, runtime)),
                ]
                try:
                    async for raw in ws:
                        data = json.loads(raw)
                        await _prime_handle_command(ws, data, runtime, worker_name)
                finally:
                    for t in tasks:
                        t.cancel()

        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def _prime_heartbeat(ws) -> None:
    import asyncio
    import json
    while True:
        await asyncio.sleep(30)
        try:
            await ws.send(json.dumps({"type": "heartbeat"}))
        except Exception:
            break
