# Claude Fleet

CLI + TUI + web dashboard for orchestrating long-running Claude Code instances on cloud VMs or local Docker containers. Spawn workers, send prompts, stream logs, collect results — all through a central server that workers auto-register with.

## Architecture

```
┌──────────────────────────────────────────────────┐
│  Central Server (`cfleet serve`)                 │
│  ├── REST API ← CLI / TUI / web dashboard       │
│  ├── WebSocket hub ← workers connect here        │
│  ├── Token auth (shared secret)                  │
│  └── State (machines + workers in state.json)    │
└────────┬──────────────────┬──────────────────────┘
         │ WebSocket         │ WebSocket
    ┌────▼────┐         ┌───▼─────┐
    │ Worker  │         │ Worker  │    Workers connect OUTBOUND
    │ relay   │         │ relay   │    (no inbound ports needed)
    │ box1    │         │ box2    │
    └─────────┘         └─────────┘
```

**Machines** are the compute layer — cloud VMs (Azure, GCP) or local Docker containers. Each machine can host multiple **workers**. A worker is a relay process wrapping the Claude Code SDK that receives prompts and streams responses.

Workers connect outbound to the central server via WebSocket. The server routes commands (ask, interrupt, status) to workers and streams events back to the CLI/TUI/web dashboard. SSH is only used for initial provisioning, file transfer (`send`/`collect`), and interactive debugging (`attach`).

## Prerequisites

### For local containers (devcontainer provider)

| Tool   | Version | Purpose           |
|--------|---------|-------------------|
| Python | 3.11+   | Runtime           |
| Docker | 24+     | Container runtime |

### For cloud VMs (Azure / GCP)

| Tool      | Version | Purpose                  |
|-----------|---------|--------------------------|
| Python    | 3.11+   | Runtime                  |
| Pulumi    | 3.x     | Infrastructure management|
| SSH key   | —       | VM access                |
| rsync     | —       | File transfer            |

Cloud provider CLIs:

| Provider  | CLI      | Auth                                                          |
|-----------|----------|---------------------------------------------------------------|
| **Azure** | `az`     | `az login`                                                    |
| **GCP**   | `gcloud` | `gcloud auth login && gcloud auth application-default login`  |

## Install

```bash
git clone https://github.com/AmeanAsad/claude-fleet.git
cd claude-fleet
pip install -e .
```

## Quickstart

### 1. Initialize

```bash
# Create a fleet.yml with your settings (optional — init will prompt)
cat > fleet.yml << 'EOF'
anthropic_api_key: "sk-ant-..."
cloud:
  provider: devcontainer   # or: azure, gcp
EOF

cfleet init
```

This creates `~/.cfleet/` with config, secrets, CLAUDE.md, and skills directory. For cloud providers, initializes a Pulumi stack. For devcontainer, builds the Docker image.

### 2. Start the server

```bash
cfleet serve
```

Auto-generates a registration token on first run and prints it. Workers use this token to authenticate when connecting. The server runs on port 8420 by default.

### 3. Spawn a worker

```bash
cfleet spawn my-worker
```

This auto-creates a machine (VM or container), bootstraps it with Claude Code and dependencies, provisions the worker, and starts a relay that connects to the server.

### 4. Send prompts and monitor

```bash
cfleet ask my-worker "Build a REST API with auth and tests"
cfleet logs my-worker -f          # stream logs live
cfleet status my-worker           # detailed status
cfleet ls                         # list all workers
```

### 5. Interact and transfer files

```bash
cfleet attach my-worker           # interactive shell on the machine
cfleet send my-worker ./data      # copy files to /workspace/inbox/
cfleet collect my-worker ./output # copy /workspace/outbox/ to local
```

### 6. Tear down

```bash
cfleet kill my-worker             # destroy worker
cfleet kill my-worker --purge     # remove from state if unreachable
cfleet kill --all                 # destroy everything
```

## Concepts

### Machines

A machine is the compute host — a cloud VM or Docker container. Machines are created explicitly with `cfleet machine create` or auto-created when you `cfleet spawn` without specifying one.

```bash
# Explicit machine management
cfleet machine create box1 --provider gcp --region us-central1-a
cfleet machine create box2 --provider azure --type snp  # confidential VM
cfleet machine ls
cfleet machine ssh box1       # interactive SSH
cfleet machine rm box1        # destroy VM + all workers on it
cfleet machine rm box1 --purge   # remove from state if VM already gone
```

One machine can host multiple workers (except devcontainer, which is 1:1). Each worker gets a unique relay port (8421, 8422, ...).

### Workers

A worker is a Claude Code agent relay running on a machine. It wraps the Claude Code SDK and connects to the central server via WebSocket.

```bash
# Spawn on an existing machine
cfleet spawn agent-1 --machine box1
cfleet spawn agent-2 --machine box1   # second worker on same machine

# Spawn with auto-created machine
cfleet spawn agent-3                  # creates machine-agent-3 automatically
cfleet spawn agent-4 --provider gcp   # auto-create a GCP VM
```

Workers accept prompts, run them through Claude Code, and stream back results. The full conversation history (thinking, tool calls, responses) is available via `logs` and `messages`.

### Central Server

The server is the communication hub. Workers connect outbound to it via WebSocket. The CLI, TUI, and web dashboard all talk to the server's REST API.

```bash
# Start the server (first run generates a token)
cfleet serve

# Connect a CLI to a remote server
cfleet connect http://my-server:8420 --token <TOKEN>

# Disconnect (stop routing through server)
cfleet disconnect
```

The server handles:
- Worker registration and heartbeats
- Command routing (ask, interrupt) to workers via WebSocket
- Event streaming from workers to clients via SSE
- State management (tracks machines, workers, tasks)
- Token authentication for all API endpoints

## Multi-Provider Support

All three providers share the same engine and state model. Mix freely:

```bash
cfleet spawn local-dev                    # devcontainer (default)
cfleet spawn cloud-worker --provider gcp  # GCP VM
cfleet spawn secure-agent --provider azure --type snp  # Azure confidential VM

cfleet ls   # shows all workers regardless of provider
```

### Provider Comparison

|                     | devcontainer     | azure            | gcp              |
|---------------------|------------------|------------------|------------------|
| **Requires**        | Docker           | `az` + subscription | `gcloud` + project |
| **Speed**           | ~30s             | ~3-5 min         | ~3-5 min         |
| **Cost**            | Free (local CPU) | Pay per VM       | Pay per VM       |
| **Isolation**       | Container        | Full VM          | Full VM          |
| **Multi-worker**    | No (1:1)         | Yes              | Yes              |
| **Confidential VMs**| N/A             | SNP, TDX         | SNP, TDX         |

### Default Instance Types

| Provider  | regular         | snp (AMD SEV-SNP)  | tdx (Intel TDX)   |
|-----------|-----------------|---------------------|--------------------|
| **Azure** | Standard_D2s_v5 | Standard_DC4as_v5  | Standard_DC4es_v6  |
| **GCP**   | e2-standard-2   | n2d-standard-2     | c3-standard-4     |

Override with `--instance-type`:

```bash
cfleet machine create big-box --provider gcp --instance-type n2d-standard-8
```

## Configuration

All config lives in `~/.cfleet/`:

| File               | Purpose                                              |
|--------------------|------------------------------------------------------|
| `config.yml`       | API keys, provider settings, model, server config    |
| `secrets.env`      | Env vars sourced on every worker                     |
| `CLAUDE.md`        | System instructions for all workers                  |
| `skills/`          | Custom skills synced to workers                      |
| `mcp-servers.json` | MCP server config for workers                        |
| `state.json`       | Machine + worker inventory (auto-managed)            |

### config.yml Reference

```yaml
# Required
anthropic_api_key: "sk-ant-..."

# Optional — defaults shown
model: claude-opus-4-6

# Cloud provider settings
cloud:
  provider: devcontainer   # devcontainer | azure | gcp
  region: ""               # provider default if empty
  vm_type: regular         # regular | snp | tdx
  instance_type: ""        # provider default if empty
  ssh_key: ~/.ssh/id_ed25519
  ssh_user: ""             # provider default (ubuntu for cloud, vscode for devcontainer)
  azure:
    subscription_id: ""
    resource_group: ""     # auto-generated during init
  gcp:
    project_id: ""
    zone: us-central1-a

# Central server
server:
  url: ""                  # set via `cfleet connect`
  host: 0.0.0.0            # bind address for `cfleet serve`
  port: 8420
  token: ""                # auto-generated on first `cfleet serve`

# Repos to clone on workers
repos:
  - name: my-app
    url: https://github.com/org/my-app.git
    branch: main

# Pulumi (auto-configured, rarely needs manual editing)
pulumi:
  project: claude-fleet
  stack: default
  backend: "file://~/.cfleet/pulumi-state"
```

### CLAUDE.md

Instructions placed in `~/.cfleet/CLAUDE.md` are deployed to every worker as `/workspace/CLAUDE.md`. Claude Code reads this file automatically. Use it to give all workers shared context about your codebase, conventions, or tasks.

### secrets.env

Environment variables in `~/.cfleet/secrets.env` are sourced on every worker. The `ANTHROPIC_API_KEY` is set here automatically during init. Add other secrets as needed:

```bash
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=ghp_...
DATABASE_URL=postgres://...
```

### Skills

Files in `~/.cfleet/skills/` are synced to `~/.claude/skills/` on each worker. These are Claude Code skills that workers can invoke.

## Graceful Cleanup and State Management

Fleet state is stored in `~/.cfleet/state.json`. It tracks all machines and workers as the single source of truth.

### Handling unreachable resources

If a machine's VM was deleted externally (via cloud console, another tool, or just died), or a worker's relay process is dead, `kill` and `machine rm` will **still succeed** — cleanup failures are always treated as warnings, never blockers. State removal always happens.

```bash
# Normal cleanup: stops relay, removes from state
cfleet kill my-worker

# Purge: removes from state without attempting any remote cleanup
# Use when you know the resource is already gone
cfleet kill stale-agent --purge
cfleet machine rm dead-box --purge
```

The `--purge` flag skips all SSH, Docker, and Pulumi operations — it only modifies `state.json`. This is the fastest way to clean up stale entries.

### Keep VM but remove from fleet

```bash
cfleet machine rm box1 --keep-vm
```

Removes the machine and workers from fleet state but leaves the cloud VM running. Useful for debugging or migrating a VM to another fleet.

## Web Dashboard

```bash
cfleet serve                    # starts on http://0.0.0.0:8420
cfleet serve --port 9000        # custom port
cfleet serve --new-token        # regenerate the auth token
```

The web dashboard provides a browser-based UI with:
- Live worker status and log streaming
- Spawn/kill controls
- Prompt input
- Token-authenticated API

Accessible from any device on the network (phone, tablet, other machines).

## TUI

```bash
cfleet tui
```

Three-panel interactive terminal UI:
- **Left**: Worker list with live status indicators
- **Right top**: Worker detail (machine, model, repos, status)
- **Right bottom**: Live conversation logs
- **Bottom**: Prompt input bar

Keybindings: **s** spawn, **k** kill, **a** attach, **i** interrupt, **f** send files, **c** collect, **q** quit.

## Command Reference

### Fleet

| Command                          | Description                                         |
|----------------------------------|-----------------------------------------------------|
| `cfleet init [-c config.yml]`    | Initialize `~/.cfleet/` directory and provider      |
| `cfleet serve [--port N]`        | Start the central fleet server                      |
| `cfleet connect <url> [--token]` | Connect CLI to a remote server                      |
| `cfleet disconnect`              | Disconnect from server                              |
| `cfleet tui`                     | Launch interactive terminal UI                      |

### Workers

| Command                                     | Description                              |
|---------------------------------------------|------------------------------------------|
| `cfleet spawn <name> [--machine M] [opts]`  | Create a worker on a machine             |
| `cfleet ls`                                 | List all workers with status             |
| `cfleet ask <name> "<prompt>"`              | Send a prompt to a worker                |
| `cfleet logs <name> [-f] [-n N]`            | Show/stream worker conversation logs     |
| `cfleet status <name>`                      | Detailed worker info                     |
| `cfleet interrupt <name>`                   | Cancel the current agent run             |
| `cfleet attach <name>`                      | Interactive shell on the worker's machine|
| `cfleet send <name> <path> [--to dest]`     | Copy files to worker                     |
| `cfleet collect <name> <dest> [--path src]` | Copy files from worker                   |
| `cfleet kill <name> [--purge] [--collect]`  | Destroy a worker                         |
| `cfleet kill --all`                         | Destroy all workers and machines         |

### Machines

| Command                                    | Description                              |
|--------------------------------------------|------------------------------------------|
| `cfleet machine create <name> [opts]`      | Create a machine (VM or container host)  |
| `cfleet machine ls`                        | List all machines                        |
| `cfleet machine ssh <name>`                | Interactive SSH into a machine           |
| `cfleet machine rm <name> [--purge]`       | Remove a machine and its workers         |

### Spawn Options

```
--machine, -M   Machine to spawn on (auto-creates if omitted)
--model, -m     Override default model (e.g., claude-sonnet-4-6)
--repo, -r      Repos to clone (repeatable, defaults to all from config)
--provider, -p  Provider for auto-created machine: devcontainer, azure, gcp
--type          VM type: regular, snp, tdx (cloud only)
--instance-type Override machine SKU (cloud only)
--region        Override default region (cloud only)
```

### Machine Create Options

```
--provider, -p    Provider: devcontainer, azure, gcp
--type            VM type: regular, snp, tdx
--instance-type   Override machine SKU
--region          Override default region
```

## REST API

The server exposes a REST API at `http://<host>:<port>/api/`. All endpoints require a bearer token (set via config or `FLEET_API_TOKEN` env var).

### Endpoints

| Method | Path                              | Description                     |
|--------|-----------------------------------|---------------------------------|
| GET    | `/api/server/info`                | Server version, connected count |
| GET    | `/api/config`                     | Fleet configuration             |
| GET    | `/api/machines`                   | List machines                   |
| GET    | `/api/workers`                    | List workers (with `connected` flag) |
| GET    | `/api/workers/{name}`             | Worker detail + relay status    |
| POST   | `/api/workers`                    | Spawn a worker (async task)     |
| DELETE | `/api/workers/{name}[?purge=true]`| Kill a worker (async task)      |
| POST   | `/api/workers/{name}/ask`         | Send prompt to worker           |
| POST   | `/api/workers/{name}/interrupt`   | Interrupt worker's agent run    |
| GET    | `/api/workers/{name}/messages`    | Get conversation history        |
| GET    | `/api/workers/{name}/usage`       | Token/cost usage for worker     |
| GET    | `/api/workers/{name}/logs`        | SSE stream of worker logs       |
| GET    | `/api/workers/{name}/logs/snapshot` | Static log snapshot           |
| GET    | `/api/tasks`                      | List background tasks           |
| GET    | `/api/tasks/{id}`                 | Get task status                 |

### WebSocket Protocol

Workers connect to `ws://<host>:<port>/ws` and follow this protocol:

1. **Register**: Worker sends `{type: "register", token: "...", worker_name: "...", machine_name: "..."}`
2. **Server confirms**: `{type: "registered", worker_name: "..."}`
3. **Heartbeat**: Worker sends `{type: "heartbeat"}` every 30s, server replies `{type: "heartbeat_ack"}`
4. **Commands**: Server sends `{type: "ask"|"interrupt"|"status"|"messages", request_id: "...", ...}`
5. **Responses**: Worker replies `{type: "response", request_id: "...", data: {...}}`
6. **Events**: Worker streams `{type: "event", data: {...}}` for log/message updates
7. **Status updates**: Worker sends `{type: "status_update", status: "idle"|"working"|"error"}`

## Data Model

### state.json Structure

```json
{
  "machines": {
    "box1": {
      "name": "box1",
      "provider": "gcp",
      "ip": "34.56.78.90",
      "region": "us-central1-a",
      "instance_type": "e2-standard-2",
      "vm_type": "regular",
      "ssh_user": "ubuntu",
      "container_id": "",
      "status": "ready",
      "worker_names": ["agent-1", "agent-2"],
      "next_relay_port": 8423,
      "created_at": "2026-05-25T..."
    }
  },
  "workers": {
    "agent-1": {
      "name": "agent-1",
      "machine_name": "box1",
      "relay_port": 8421,
      "model": "claude-opus-4-6",
      "repos": ["my-app"],
      "status": "idle",
      "session_id": "uuid-...",
      "created_at": "2026-05-25T...",
      "last_prompt": "Build the auth module",
      "last_prompt_at": "2026-05-25T..."
    }
  }
}
```

### Machine States

| State          | Meaning                                |
|----------------|----------------------------------------|
| `creating`     | VM being provisioned via Pulumi        |
| `provisioning` | Bootstrap script running (packages, Claude Code) |
| `ready`        | Machine is ready to accept workers     |
| `errored`      | Machine creation or provisioning failed|
| `stopped`      | Machine was stopped                    |

### Worker States

| State          | Meaning                                |
|----------------|----------------------------------------|
| `spawning`     | Worker entry created, setup starting   |
| `provisioning` | Worker relay being deployed            |
| `idle`         | Worker is ready, no prompt running     |
| `working`      | Agent is processing a prompt           |
| `errored`      | Agent encountered an error             |
| `stopped`      | Worker was stopped                     |

## Provisioning

Fleet uses bash scripts (no Ansible) for machine provisioning:

### Machine Bootstrap (runs once per machine)
- Installs system packages (git, curl, rsync, ripgrep, jq, etc.)
- Installs Claude Code CLI (standalone binary)
- Configures Claude Code (API key, settings, onboarding skip)
- Installs Python relay dependencies (FastAPI, uvicorn, claude-code-sdk)
- Deploys the worker relay script

### Worker Provisioning (runs per worker on a machine)
- Creates workspace directory (`/workspace/{worker_name}/`)
- Clones configured repos (with push disabled)
- Deploys CLAUDE.md, skills, MCP config, secrets
- Creates a systemd service `cfleet-relay-{worker_name}` on the assigned port
- Starts the relay and waits for health check

For devcontainers, provisioning happens inside the Docker container via `docker exec`.

## Troubleshooting

### Worker shows as "not connected"

The worker relay couldn't reach the server. Check:
1. Is the server running? (`cfleet serve`)
2. Can the worker reach the server URL? (check firewall/network)
3. Is the token correct? (check `CFLEET_TOKEN` env var on the worker)

### Stale state after cloud VM deletion

If you deleted a VM outside of cfleet (via cloud console, etc.):

```bash
cfleet kill worker-name --purge
cfleet machine rm machine-name --purge
```

The `--purge` flag removes entries from `state.json` without attempting any remote operations.

### Pulumi state lock

If Pulumi reports a state lock:
```bash
cd ~/.cfleet/pulumi-state
# Remove the lock file for the stuck operation
```

### Relay not starting on cloud VM

SSH into the machine and check:
```bash
cfleet machine ssh box1
systemctl status cfleet-relay-worker-name
journalctl -u cfleet-relay-worker-name -f
```

## License

MIT
