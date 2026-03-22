# Claude Fleet

CLI for orchestrating long-running Claude Code instances on cloud VMs or local Docker containers. Spawn workers, send prompts, stream logs, collect results.

## Prerequisites

### For local containers (devcontainer provider)

| Tool | Version | Install | Purpose |
|------|---------|---------|---------|
| Python | 3.11+ | [python.org](https://www.python.org/downloads/) | Runtime |
| Docker | 24+ | [docker.com](https://docs.docker.com/get-docker/) | Container runtime |

That's it. No cloud account, no Pulumi, no SSH keys needed.

### For cloud VMs (azure/gcp providers)

| Tool | Version | Install | Purpose |
|------|---------|---------|---------|
| Python | 3.11+ | [python.org](https://www.python.org/downloads/) | Runtime |
| Pulumi CLI | 3.x | `brew install pulumi` or [pulumi.com/docs/install](https://www.pulumi.com/docs/install/) | Infrastructure management |
| SSH key | — | `ssh-keygen -t ed25519` | VM access |
| rsync | — | Pre-installed on macOS/Linux | File transfer |
| Ansible | 2.15+ | Installed as dependency | VM provisioning |

### Cloud provider CLIs (install the ones you need)

| Provider | CLI | Install | Auth |
|----------|-----|---------|------|
| **Azure** | `az` | `brew install azure-cli` or [docs](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) | `az login` |
| **GCP** | `gcloud` | `brew install google-cloud-sdk` or [docs](https://cloud.google.com/sdk/docs/install) | `gcloud auth login && gcloud auth application-default login` |

## Install

```bash
# With uv (recommended)
git clone https://github.com/AmeanAsad/claude-fleet.git
cd claude-fleet
uv sync
uv run cfleet --help

# With pip
git clone https://github.com/AmeanAsad/claude-fleet.git
cd claude-fleet
pip install -e .
```

## Quickstart (local containers)

No cloud account needed. Just Docker and an API key.

```bash
# 1. Configure
cp fleet.yml.example fleet.yml
nano fleet.yml                   # add your Anthropic API key

# 2. Initialize — auto-detects devcontainer if no cloud CLI installed
cfleet init

# 3. Spawn a local worker
cfleet spawn dev

# 4. Send it a task
cfleet ask dev "Set up a FastAPI project with auth, tests, and Docker"

# 5. Watch it work
cfleet logs dev -f

# 6. Jump in interactively (Ctrl+B d to detach)
cfleet attach dev

# 7. Pull results back to your machine
cfleet collect dev ./output

# 8. Tear it down when done
cfleet kill dev
```

## Multi-provider support

All three providers can be used simultaneously. Set a default in config and override per-worker:

```bash
# Uses default provider from config
cfleet spawn worker1

# Override provider for this worker
cfleet spawn worker2 --provider gcp
cfleet spawn worker3 --provider devcontainer

# Mix providers freely
cfleet spawn worker4 -p gcp --type snp    # GCP confidential VM (AMD SEV-SNP)
cfleet spawn worker5 -p azure --type tdx   # Azure confidential VM (Intel TDX)
cfleet spawn worker6 -p devcontainer       # Local Docker container
```

### fleet.yml example

```yaml
anthropic_api_key: "sk-ant-..."

# Local containers — no cloud config needed
cloud:
  provider: devcontainer

# Or cloud providers
# cloud:
#   provider: azure
#   azure:
#     subscription_id: "your-azure-subscription-id"
#   gcp:
#     project_id: "your-gcp-project-id"
```

### Provider comparison

| | devcontainer | azure | gcp |
|---|---|---|---|
| **Requires** | Docker | `az` CLI + subscription | `gcloud` CLI + project |
| **Speed** | ~30s | ~3-5 min | ~3-5 min |
| **Cost** | Free (local CPU) | Pay per VM | Pay per VM |
| **Isolation** | Container sandbox | Full VM | Full VM |
| **Confidential VMs** | N/A | SNP, TDX | SNP, TDX |
| **Use case** | Dev, testing, solo | Production, teams | Production, teams |

### Default instance types (cloud providers)

| Provider | regular | snp (AMD SEV-SNP) | tdx (Intel TDX) |
|----------|---------|---------------------|-----------------|
| **Azure** | Standard_D2s_v5 | Standard_DC4as_v5 | Standard_DC4es_v6 |
| **GCP** | e2-standard-2 | n2d-standard-2 | c3-standard-4 |

## Lifecycle

### 1. Initialize

```bash
cp fleet.yml.example fleet.yml
# Add your Anthropic API key (and cloud provider details if using cloud)
cfleet init
```

This creates `~/.cfleet/` with your config and secrets. For cloud providers, it also initializes a Pulumi stack. For devcontainer, it builds the Docker image. If you skip `fleet.yml`, `init` will prompt interactively — defaulting to `devcontainer` if no cloud CLI is detected.

### 2. Spawn workers

```bash
cfleet spawn my-worker
```

For devcontainer: starts a Docker container with Claude Code pre-installed.
For cloud: provisions a VM, installs Claude Code via Ansible, clones repos, and starts a tmux session.

Options:

```bash
cfleet spawn my-worker \
  --provider devcontainer \            # devcontainer | azure | gcp
  --type snp \                         # regular | snp | tdx (cloud only)
  --model claude-opus-4-6 \            # override default model
  --instance-type n2d-standard-4 \     # override machine type (cloud only)
  --repo myapp --repo infra \          # specific repos (default: all from config)
  --region us-east1-b                  # override default region (cloud only)
```

### 3. Send prompts

```bash
cfleet ask my-worker "Build an OAuth login flow for the API"
```

Fire and forget — the worker picks up the prompt and starts working. Check progress with `ls` or `logs`.

### 4. Monitor

```bash
cfleet ls                    # List all workers with status
cfleet status my-worker      # Detailed info (IP/container, uptime, tmux state)
cfleet logs my-worker        # Last 100 lines of tmux output
cfleet logs my-worker -f     # Stream logs continuously
```

### 5. Interact directly

```bash
cfleet attach my-worker      # Attach to the worker's tmux session (Ctrl+B d to detach)
```

<p align="center">
  <img src="docs/attach.png" alt="Claude Fleet attach" width="500">
</p>

### 6. Transfer files

```bash
cfleet send my-worker ./local-dir         # Copy files to /workspace/inbox/
cfleet collect my-worker ./output          # Copy /workspace/outbox/ to local
```

### 7. Tear down

```bash
cfleet kill my-worker                     # Destroy a single worker
cfleet kill --all                         # Destroy all workers
cfleet kill my-worker --collect ./backup  # Collect files before destroying
```

## TUI

```bash
cfleet tui
```

Three-panel interactive terminal UI. Select workers from the left, view detail and live logs on the right, send prompts from the bottom input bar.

Keybindings: **s** spawn, **k** kill, **a** attach, **f** send files, **c** collect, **q** quit.

<p align="center">
  <img src="docs/tui.png" alt="Claude Fleet TUI" width="700">
</p>

## Web Dashboard

```bash
cfleet serve                  # http://localhost:8420
cfleet serve --port 9000      # custom port
```

Browser-based dashboard with live log streaming, spawn/kill controls, and prompt input. Accessible from any device on the network.

Set `api.token` in `~/.cfleet/config.yml` or export `FLEET_API_TOKEN` to require authentication.

<p align="center">
  <img src="docs/dashboard.png" alt="Claude Fleet Dashboard" width="700">
</p>

## Configuration

All config lives in `~/.cfleet/`:

| File               | Purpose                                              |
| ------------------ | ---------------------------------------------------- |
| `config.yml`       | API keys, provider settings, model defaults          |
| `secrets.env`      | Env vars sourced on every worker                     |
| `CLAUDE.md`        | System instructions for all workers                  |
| `skills/`          | Custom skills synced to workers                      |
| `mcp-servers.json` | MCP server config for workers                        |
| `state.json`       | Worker inventory (auto-managed, single source of truth) |

## Architecture

- **State**: `~/.cfleet/state.json` is the single canonical source of truth for all worker state — cloud VMs and local containers alike. Each worker tracks its `provider`, and cloud workers additionally have Pulumi-managed infra while devcontainer workers track a `container_id`.
- **Providers**: Three providers share the same state and engine interface:
  - **devcontainer** — Docker containers based on [Trail of Bits' claude-code-devcontainer](https://github.com/trailofbits/claude-code-devcontainer). Provisioned inline via `docker exec`. No Pulumi.
  - **azure** / **gcp** — Cloud VMs via Pulumi inline program. Provisioned via Ansible over SSH.
- **Engine**: `FleetEngine` dispatches to provider-specific spawn/kill paths but uses a unified connection interface (`WorkerSSH` or `WorkerDocker`) for all runtime operations (ask, attach, logs, send, collect).

## Command Reference

| Command | Description |
| --- | --- |
| `cfleet init` | Set up `~/.cfleet/` and initialize provider |
| `cfleet spawn <name> [-p provider]` | Create a worker (VM or container) |
| `cfleet ls` | List all workers |
| `cfleet ask <name> "<prompt>"` | Send a prompt (fire and forget) |
| `cfleet attach <name>` | Attach to worker's tmux session |
| `cfleet logs <name> [-f] [-n N]` | Show/stream worker output |
| `cfleet status <name>` | Detailed worker info |
| `cfleet send <name> <path>` | Copy files to worker |
| `cfleet collect <name> <dest>` | Copy files from worker |
| `cfleet kill <name> [--all] [--force]` | Destroy worker(s) |
| `cfleet serve [--port N]` | Start web dashboard + REST API |
| `cfleet tui` | Launch interactive TUI |

## License

MIT
