# Prime-Agent Backend for claude-fleet ("prime mode")

> Status: design + implementation plan. Branch: `feat/prime-agent-backend`.
> Research: `plans/prime-agent-capabilities.md`, `plans/cfleet-architecture-map.md`.

## Goal

Let a fleet worker run a **prime-agent** session instead of a Claude Code session,
while keeping every existing fleet surface working unchanged: `cfleet
spawn/ask/logs/status/interrupt/attach/kill`, the central server, the TUI, and the
web dashboard.

User requirements this design is built around:

- **Persist** worker conversations across relay/machine restarts.
- **Resume** a worker's conversation after stop/restart (same identity, full history).
- **Send data** (prompts) into running sessions from the fleet control plane.
- Use prime-agent's **native** daemon/session/message functionality — no hand-rolled
  session store, no reimplemented agent loop.

## Why this works cleanly

The cfleet server is **backend-agnostic**. A worker is any process that speaks the
worker WebSocket contract (`/ws`: `register`, `heartbeat`, `event`, `status_update`,
`response`; commands `ask`, `messages`, `status`, `interrupt`) and reports messages
in the dashboard's normalized schema (`UserPrompt`/`AssistantMessage` with
`TextBlock`/`ThinkingBlock`/`ToolUseBlock`/`ToolResultBlock`). Today that contract is
spoken by `cfleet agent`, which wraps `claude_code_sdk`. Prime mode adds a second
implementation of the same contract that drives a **resident prime-agent daemon
session** through prime-agent's public CLI.

No server, dashboard, or TUI changes are required beyond passing one new field
(`agent_backend`) through spawn.

## Prime-agent primitives used (all validated end-to-end on v0.7.2)

| Fleet need | Prime-agent native mechanism |
|---|---|
| Create named session, headless | `prime-agent --mode rpc` → RPC `set_heartbeat` (promotes client-owned session to **resident daemon worker**) → close RPC → `prime-agent rename <activeId> <worker>` → `prime-agent schedule cancel <heartbeat-job>` |
| Send prompt (data in) | `prime-agent send <worker> "<prompt>"` — daemon-routed agent message. **Wakes saved/archived sessions into resident workers automatically** (daemon catalog resolve → createOrReuseWorker). |
| Live status | `prime-agent list --json` (per-session `lifecycle`, `activity`, `messageCount`, `sessionFile`) |
| History / messages | Session JSONL at `~/.prime/agent/sessions/<uuid>.jsonl` (documented v3 tree format) |
| Persistence | Sessions are plain JSONL + `session-artifacts/<uuid>/`; daemon workers survive client detach; supervisor restart adopts workers; saved sessions re-wake on `send` |
| Stop | `prime-agent stop <worker>` (session archived, JSONL kept, resumable) |
| Attach (operator TUI) | `prime-agent attach <worker>` (native) |
| Scheduling (bonus) | `prime-agent schedule add <worker> "cron" -- "prompt"` works out of the box |

### Validated lifecycle (local probes, 2026-08-14)

1. RPC create + `set_heartbeat` → session becomes resident (`activeSessionId` issued).
2. `rename` to `fleet-probe2`, cancel heartbeat → session stays live, no recurring job.
3. `prime-agent stop` → archived; JSONL intact.
4. `prime-agent send fleet-probe2 "..."` → session **auto-woke** (new activeSessionId),
   full history preserved, reply landed in JSONL.
5. `--mode rpc --resume <session-id>` also re-opens with full history (alternative path).

Notes:
- A fresh, never-prompted session lists as lifecycle `draft` with 0 messages — normal.
- `send` delivers as a `custom_message` JSONL entry (`customType: "agent_message"`);
  the clean prompt text is at `details.message`.
- Daemon default `idleEvictionMinutes=90` evicts idle workers; next `send` re-wakes.
  This is a feature (resource hygiene) and invisible to the fleet.
- No public API to abort another session's in-flight turn. `interrupt` returns an
  explicit "unsupported for prime backend" error in v1 (see Non-goals).

## Design

### New module: `src/cfleet/prime_backend.py`

Owns every prime-agent interaction. Pure subprocess + JSONL parsing; no new deps.

```
class PrimeAgentBackend:
    __init__(worker_name, cwd, model=None)
    ensure_session()  -> PrimeSessionInfo   # adopt live → adopt saved → create fresh
    send(prompt)      -> None               # prime-agent send
    stop()            -> None               # prime-agent stop
    status()          -> dict               # list --json entry + message count
    read_messages(before, limit) -> dict    # paginated, reverse-tail read
    jsonl_path        -> Path | None
prime_entry_to_message(entry) -> dict|None  # prime JSONL → dashboard Message
```

`ensure_session()` adoption order (idempotent, safe to call on every relay boot):
1. `prime-agent list --all --json` → live/draft session named exactly `<worker>` → adopt.
2. Else saved session named `<worker>` → adopt (first `send` wakes it).
3. Else create: RPC spawn recipe above. Session name = worker name. `--cwd` = worker
   dir, `--model` passed only when explicitly configured.

Marker file `.cfleet-worker` in the worker dir gains:
`{"name", "backend": "prime", "prime_session_id", "prime_session_file"}`.
(Existing claude markers without `backend` read as `"claude"`.)

### Worker process: `cfleet agent --backend prime`

Same WS loop as today; three pieces are swapped:

- `_agent_run_sdk` → **send-and-watch**: `prime-agent send`, then poll
  `prime-agent list --json` (2.5 s) mirroring `activity` (`working`/`idle`) into fleet
  `status_update`s. Relay crash = harmless: session keeps running; the respawned
  relay re-adopts and re-syncs status on next poll.
- Claude JSONL tailer → **prime JSONL tailer** on the session file (0.5 s poll,
  byte-offset, convert via `prime_entry_to_message`). Bootstraps from offset 0 like
  the claude tailer; the dashboard dedupes (role|type|block-summaries).
- `_read_session_messages` → prime reader (same reverse-tail pagination, prime format).

Message conversion (`prime_entry_to_message`):

| prime entry | dashboard Message |
|---|---|
| `message` role=user | `UserPrompt`, `TextBlock(text)` |
| `custom_message` customType=agent_message | `UserPrompt`, `TextBlock(details.message)` |
| `message` role=assistant | `AssistantMessage`; `thinking`→`ThinkingBlock`, `text`→`TextBlock`, `toolCall`→`ToolUseBlock(tool_name=name, tool_input=arguments)`; appends usage line |
| `message` role=toolResult | role=user `UserPrompt` with `ToolResultBlock(content, is_error)` (mirrors claude tool-result rendering) |
| everything else (session/model_change/agent_status/compaction/…) | skipped (`None`) |

### Spawn plumbing (one new field, end to end)

- `cfleet spawn --backend prime` → `FleetEngine.spawn(agent_backend=...)` →
  `WorkerState.agent_backend` → `POST /api/machines/{m}/spawn {…, agent_backend}` →
  WS `spawn_worker` → `MachineAgent._launch_worker` appends `--backend prime`.
- Server `GET /api/machines/{m}/workers?detail=true` includes `agent_backend` so
  reboot-respawn preserves the backend.
- `/ws` worker registration accepts optional `agent_backend` (manual launches).
- Dashboard spawn (`POST /api/workers`) accepts `agent_backend` (optional; default claude).

### Kill / restart semantics

- `cfleet kill <name>`: machine agent terminates the relay process, then — for prime
  workers — best-effort `prime-agent stop <name>`. With `purge_session=True` it also
  deletes the prime session JSONL + `session-artifacts/<id>/`.
- `cfleet restart <name>`: kill with `purge_session=False` → the prime session is
  *not* stopped (only the relay dies) → respawned relay adopts the still-live (or
  saved) session by name. Conversation, kernel state (while resident), schedules,
  and RLM children all survive.

### Attach

`cfleet attach <prime-worker>` → SSH to machine → `prime-agent attach <worker-name>`
(native resident-session attach; Ctrl+B d-style detach returns, session keeps running).

### Auth / models

Prime-agent auth is per-machine (`~/.prime/agent/auth.json` or env vars like
`ANTHROPIC_API_KEY` / `PRIME_API_KEY`); prime-agent reads its own settings for the
default model. `cfleet spawn --backend prime --model kimi-k3` passes `--model` through
to prime-agent at session creation. When no model is given, prime-agent's own default
is used (unlike claude workers, no API key is injected by the fleet).

## Non-goals (v1)

- **Interrupt**: no supported public API to abort a live turn of another session.
  The fleet `interrupt` command returns a clear unsupported error for prime workers.
  Workarounds: `prime-agent send <w> --steer "…"` from the machine, or `cfleet kill`.
- Live token-level streaming: events are forwarded at message granularity (same as
  the claude TUI-authored path). A future v1.1 can attach an RPC `observe` client for
  delta streaming.
- Cross-machine session migration (copying JSONL between hosts). The format is
  portable (validated by explorer) but no fleet UX is built for it yet.
- devcontainer provider (already broken for claude; unchanged).

## Testing

1. **Unit**: `prime_entry_to_message` over real captured JSONL fixtures; reader
   pagination; `ensure_session` adoption matrix (mocked subprocess).
2. **Local E2E**: `cfleet serve` + `cfleet agent t1 --backend prime` on this Mac →
   `cfleet ls/ask/logs/status/kill`; kill −→ restart resume check (history preserved).
3. **Remote E2E (lunal-host-1, over Tailscale)**: install branch, run a prime worker
   enrolled to a test server; verify spawn/ask/logs/attach/kill + relay-restart
   adoption; verify it coexists with the existing claude workers on that host.

## Risks / gotchas

- **prime-agent version skew**: the spawn recipe depends on `send`-wakes-saved and
  RPC `set_heartbeat` promotion (both present in ≥0.7.2). The backend probes
  `prime-agent --version` at boot and warns below 0.7.2.
- **Daemon is per-OS-user**: the relay must run as the same user that owns the
  prime-agent daemon/auth on that machine.
- **Name collisions**: session name == worker name; `ensure_session` only adopts
  sessions it can uniquely resolve, and only top-level (`runtimeKind`) sessions.
- **`agent_status` chatter entries** appear in the JSONL; converter skips them.
- **`send` framing**: prompts arrive wrapped as agent-to-agent messages
  (`From: fleet …`). Workers "know" they're being addressed by the fleet. This is
  semantically accurate for an operator-driven worker and is displayed cleanly.
