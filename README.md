# Claude Fleet

CLI for orchestrating long-running Claude Code instances on Azure VMs. Spawn workers, send prompts, collect results — all over SSH + tmux + rsync.

## Prerequisites

- Python 3.11+, [Pulumi CLI](https://www.pulumi.com/docs/install/), Azure CLI (`az login`), SSH key (`~/.ssh/id_ed25519`), `rsync`

## Install

```bash
git clone https://github.com/AmeanAsad/claude-fleet.git
cd claude-fleet
pip install -e .
```

## Quick start

```bash
# Configure and initialize
cp fleet.yml.example fleet.yml   # add API key + Azure subscription ID
cfleet init

# Spawn, use, destroy
cfleet spawn my-worker
cfleet ask my-worker "Build an OAuth login flow"
cfleet logs my-worker
cfleet kill my-worker
```

## Commands

| Command | Description |
|---|---|
| `cfleet init` | Set up `~/.cfleet/` and Pulumi stack |
| `cfleet spawn <name>` | Create a worker VM |
| `cfleet ls` | List all workers |
| `cfleet ask <name> "<prompt>"` | Send a prompt (fire and forget) |
| `cfleet attach <name>` | SSH into worker's tmux session |
| `cfleet logs <name> [-f] [-n N]` | Show/stream worker output |
| `cfleet status <name>` | Detailed worker info |
| `cfleet send <name> <path>` | rsync files to worker |
| `cfleet collect <name> <dest>` | rsync files from worker |
| `cfleet kill <name> [--all]` | Destroy worker VM(s) |
| `cfleet serve [--port N]` | Start web dashboard + REST API |
| `cfleet tui` | Launch interactive TUI |

### Spawn options

```bash
cfleet spawn my-worker \
  --vm-type snp \                    # regular | snp | tdx (confidential VMs)
  --model claude-opus-4-6 \          # override model
  --instance-type Standard_DC8as_v5  # override Azure SKU
  --repo myapp --repo infra          # specific repos (default: all)
```

## Configuration

All config lives in `~/.cfleet/`:

| File | Purpose |
|---|---|
| `config.yml` | API keys, cloud settings, defaults |
| `secrets.env` | Env vars sourced on every worker |
| `CLAUDE.md` | Instructions for all workers |
| `skills/` | Custom skills synced to workers |
| `mcp-servers.json` | MCP server config for workers |
| `state.json` | Worker inventory (auto-managed) |

## License

MIT
