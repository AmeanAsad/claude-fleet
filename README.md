# Open Fleet

CLI + TUI for orchestrating long-running Claude Code instances on cloud VMs.

Spawn workers, hand them tasks, send them files, and collect results. Each worker is an isolated Azure VM with Claude Code running persistently in tmux, pre-loaded with your skills, config, and read-only copies of your repos.

## How it works

```
You (host)                          Cloud VMs (workers)
───────────                         ───────────────────
fleet spawn "auth-worker"    →      Azure VM boots, Ansible provisions,
                                    Claude Code starts in tmux

fleet ask auth-worker "..."  →      Prompt injected into Claude Code via SSH

fleet logs auth-worker       ←      tmux scrollback captured via SSH

fleet attach auth-worker     ↔      Interactive tmux session over SSH

fleet send auth-worker ./src →      rsync files to /workspace/inbox/

fleet collect auth-worker .  ←      rsync files from /workspace/

fleet kill auth-worker       →      Pulumi destroys the VM
```

No custom daemons on workers. No agent protocols. Just SSH + tmux + rsync.

## Features

- **Confidential VMs**: First-class support for AMD SEV-SNP and Intel TDX via `--vm-type snp|tdx`
- **Fire and forget**: Send a prompt, check back later. Workers run autonomously
- **Git safety**: Workers get read-only repo clones with push disabled
- **Skills & config**: Your `CLAUDE.md`, MCP servers, and skills are synced to every worker
- **Interactive TUI**: Three-panel interface for managing workers, streaming logs, and sending prompts
- **Web dashboard**: Control your fleet from a browser or phone via `fleet serve`
- **REST API**: Programmatic access to all fleet operations with SSE log streaming
- **File exchange**: `fleet send` / `fleet collect` via rsync with `.gitignore` filtering

## Prerequisites

- Python 3.11+
- [Pulumi CLI](https://www.pulumi.com/docs/install/)
- Azure CLI (`az login` done)
- An Azure subscription
- SSH key pair (`~/.ssh/id_ed25519`)
- `rsync` installed locally

## Install

```bash
# Clone and install
git clone https://github.com/AmeanAsad/open-fleet.git
cd open-fleet
pip install -e .

# Or with uv
uv pip install -e .
```

## Quick start

```bash
# 1. Create your config
cp fleet.yml.example fleet.yml
# Edit fleet.yml with your Anthropic API key and Azure subscription ID

# 2. Initialize fleet (creates ~/.fleet/, sets up Pulumi stack)
fleet init

# 3. Spawn a worker
fleet spawn my-worker                        # regular VM
fleet spawn my-worker --vm-type snp          # confidential VM (AMD SEV-SNP)

# 4. Send it work
fleet ask my-worker "Build an OAuth login flow for the Express app in /workspace/repos/myapp"

# 5. Check on it
fleet logs my-worker
fleet ls

# 6. Attach interactively (Ctrl+B d to detach)
fleet attach my-worker

# 7. Collect results
fleet collect my-worker ./output

# 8. Done — destroy the VM
fleet kill my-worker
```

## CLI reference

| Command                       | Description                                       |
| ----------------------------- | ------------------------------------------------- |
| `fleet init`                  | Create `~/.fleet/` directory, set up Pulumi stack |
| `fleet spawn <name>`          | Create a new worker VM                            |
| `fleet ls`                    | List all workers with status                      |
| `fleet ask <name> "<prompt>"` | Send a prompt to a worker (fire and forget)       |
| `fleet attach <name>`         | SSH into worker's tmux session                    |
| `fleet logs <name> [-f]`      | Show/stream worker's Claude Code output           |
| `fleet status <name>`         | Detailed worker info (IP, uptime, tmux status)    |
| `fleet send <name> <path>`    | rsync files to worker's `/workspace/inbox/`       |
| `fleet collect <name> <dest>` | rsync worker's `/workspace/` to local             |
| `fleet kill <name>`           | Destroy worker VM                                 |
| `fleet kill --all`            | Destroy all worker VMs                            |
| `fleet serve`                 | Start web dashboard (browser/phone control)       |
| `fleet tui`                   | Launch interactive TUI                            |

### Spawn options

```bash
fleet spawn my-worker \
  --vm-type snp \                    # regular | snp | tdx
  --model claude-opus-4-6 \          # override default model
  --instance-type Standard_DC8as_v5  # override Azure SKU
  --repo myapp --repo infra          # specific repos (default: all)
```

## TUI

`fleet tui` launches a three-panel interface:

```
┌─ Open Fleet ───────────────────────────────────────────┐
│                                                         │
│  Workers            │  my-worker                        │
│  ─────────          │  ──────────                       │
│  ● my-worker  idle  │  IP:    20.86.175.224             │
│  ● worker2   work   │  Model: claude-opus-4-6           │
│                     │  Repos: myapp, infra              │
│                     │                                   │
│                     │  ┌─ Output ────────────────────┐  │
│                     │  │ I've analyzed the OAuth      │  │
│                     │  │ requirements and will        │  │
│                     │  │ implement...                 │  │
│                     │  └─────────────────────────────┘  │
│                     │                                   │
│                     │  > Send prompt here...            │
│                                                         │
│  [s]pawn [k]ill [a]ttach [f]send [c]ollect [q]uit      │
└─────────────────────────────────────────────────────────┘
```

**Keybindings**: `s` spawn, `k` kill, `a` attach, `f` send files, `c` collect files, `q` quit

## Web Dashboard

`fleet serve` starts a web server you can access from your browser or phone.

```bash
fleet serve                              # http://localhost:8420
fleet serve --port 9000                  # custom port
FLEET_API_TOKEN=secret fleet serve       # with authentication
```

The dashboard provides the same controls as the TUI: view workers, stream logs, send prompts, spawn and kill workers.

### REST API

All endpoints are under `/api/`. Authentication is via `Authorization: Bearer <token>` header (or `?token=` query param for SSE).

| Method   | Path                              | Description                          |
| -------- | --------------------------------- | ------------------------------------ |
| `GET`    | `/api/workers`                    | List all workers                     |
| `GET`    | `/api/workers/{name}`             | Worker detail (IP, status, uptime)   |
| `POST`   | `/api/workers`                    | Spawn a worker (background task)     |
| `DELETE` | `/api/workers/{name}`             | Kill a worker (background task)      |
| `POST`   | `/api/workers/{name}/ask`         | Send a prompt                        |
| `GET`    | `/api/workers/{name}/logs`        | Stream logs (SSE)                    |
| `GET`    | `/api/workers/{name}/logs/snapshot` | Fetch last N lines                 |
| `GET`    | `/api/tasks`                      | List background tasks                |
| `GET`    | `/api/tasks/{id}`                 | Get task status                      |

**Spawn** request body:
```json
{"name": "my-worker", "vm_type": "snp", "model": "claude-opus-4-6"}
```

**Ask** request body:
```json
{"prompt": "Build an OAuth login flow"}
```

**Log streaming** uses Server-Sent Events. Connect with `EventSource`:
```javascript
const src = new EventSource('/api/workers/my-worker/logs?token=...');
src.addEventListener('logs', (e) => console.log(JSON.parse(e.data).content));
src.addEventListener('status', (e) => console.log(JSON.parse(e.data).idle));
```

**Spawn/kill** are long-running operations that return a `task_id` immediately. Poll `/api/tasks/{id}` to check progress.

### API config

Add to `~/.fleet/config.yml`:

```yaml
api:
  host: 0.0.0.0
  port: 8420
  token: ""          # empty = no auth; or set FLEET_API_TOKEN env var
```

## Configuration

### `~/.fleet/config.yml`

Created by `fleet init`. Controls defaults for all workers.

```yaml
anthropic_api_key: "sk-ant-..."
model: claude-opus-4-6

repos:
  - name: myapp
    url: https://github.com/me/myapp.git
    branch: main

cloud:
  provider: azure
  region: westeurope
  vm_type: regular          # regular | snp | tdx
  instance_type: Standard_D2s_v5
  ssh_key: ~/.ssh/id_ed25519
  ssh_user: azureuser
  azure:
    subscription_id: "..."
    resource_group: fleet-workers
```

### `~/.fleet/secrets.env`

Environment variables sourced on every worker:

```bash
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=ghp_...
DATABASE_URL=postgres://...
```

### `~/.fleet/CLAUDE.md`

Instructions copied to every worker's `/workspace/CLAUDE.md`. Workers follow these instructions automatically.

### `~/.fleet/skills/`

Custom skills directory copied to `~/.claude/skills/` on workers.

### `~/.fleet/mcp-servers.json`

MCP server config copied to `~/.claude/mcp-servers.json` on workers.

## Worker VM layout

```
/workspace/
├── repos/          # read-only git clones (push disabled)
│   ├── myapp/
│   └── infra/
├── inbox/          # files from `fleet send`
├── outbox/         # put results here for `fleet collect`
└── CLAUDE.md       # your instructions

~/.claude/
├── skills/         # your custom skills
├── settings.json   # pre-configured for headless operation
└── mcp-servers.json
```

## Architecture

```
fleet CLI / TUI / Web API
      │
      ▼
Fleet Engine (engine.py)
      │
      ├── Pulumi (infra.py)     → create/destroy Azure VMs
      ├── Ansible (provisioner.py) → bootstrap: deps, Claude Code, repos, skills
      └── SSH (ssh.py)          → runtime: prompts, logs, attach, rsync
```

**State is stored in three places:**

| Location                 | What                                    | Format         |
| ------------------------ | --------------------------------------- | -------------- |
| `~/.fleet/config.yml`    | Your configuration                      | YAML           |
| `~/.fleet/state.json`    | Worker inventory (IPs, status, prompts) | JSON           |
| `~/.fleet/pulumi-state/` | Azure resource state                    | Pulumi backend |

## VM types

| Type    | Flag                | Default SKU         | Description                 |
| ------- | ------------------- | ------------------- | --------------------------- |
| Regular | `--vm-type regular` | `Standard_D2s_v5`   | Standard Azure VM           |
| SNP     | `--vm-type snp`     | `Standard_DC4as_v5` | AMD SEV-SNP confidential VM |
| TDX     | `--vm-type tdx`     | `Standard_DC4es_v6` | Intel TDX confidential VM   |

## License

MIT
