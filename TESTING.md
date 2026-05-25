# Claude Fleet — Manual Test Plan

End-to-end verification of all fleet functionality on GCP and Azure.
Run these tests sequentially — later tests depend on state from earlier ones.

## Prerequisites

```bash
# GCP
gcloud auth login
gcloud auth application-default login
gcloud config set project <YOUR_PROJECT>

# Azure
az login
az account set --subscription <YOUR_SUBSCRIPTION>

# SSH key must exist
ls ~/.ssh/id_ed25519  # or whichever key you use

# Fleet installed
pip install -e .
cfleet --help
```

---

## 0. Init + Server

### 0.1 — Fresh init (GCP)

```bash
rm -rf ~/.cfleet
cat > fleet.yml << 'EOF'
anthropic_api_key: "sk-ant-YOUR_KEY"
model: claude-sonnet-4-6
cloud:
  provider: gcp
  gcp:
    project_id: "YOUR_PROJECT"
    zone: "us-central1-a"
repos:
  - name: claude-fleet
    url: https://github.com/AmeanAsad/claude-fleet.git
    branch: v1-cleanup
EOF
cfleet init -c fleet.yml
```

**Verify**:
- [ ] `~/.cfleet/` created with: `config.yml`, `secrets.env`, `CLAUDE.md`, `state.json`, `skills/`, `pulumi-state/`
- [ ] `secrets.env` contains your `ANTHROPIC_API_KEY`
- [ ] Pulumi stack initialized (no errors)

### 0.2 — Add secrets

```bash
cat >> ~/.cfleet/secrets.env << 'EOF'
GITHUB_TOKEN=ghp_YOUR_TOKEN
EOF
```

**Verify**:
- [ ] `cat ~/.cfleet/secrets.env` shows both `ANTHROPIC_API_KEY` and `GITHUB_TOKEN`

### 0.3 — Start server

```bash
cfleet serve &
```

**Verify**:
- [ ] Server starts, prints a registration token
- [ ] `curl http://127.0.0.1:8420/api/server/info` returns version and empty connected list
- [ ] Token stored in `~/.cfleet/config.yml` under `server.token`
- [ ] `server.url` is set in config

### 0.4 — Connect CLI to server

```bash
# If running on same machine, this should auto-configure during serve
# Otherwise:
cfleet connect http://127.0.0.1:8420 --token <TOKEN>
```

**Verify**:
- [ ] `grep "url" ~/.cfleet/config.yml` shows the server URL

---

## 1. GCP — Machine Lifecycle

### 1.1 — Create machine

```bash
cfleet machine create gcp-box --provider gcp --region us-central1-a
```

**Verify**:
- [ ] Pulumi provisions a GCP VM (watch for Pulumi output)
- [ ] Machine bootstrap runs (installs Claude Code, Python deps, relay script)
- [ ] `cfleet machine ls` shows `gcp-box` with status `ready` and an IP
- [ ] GCP console shows the VM running

### 1.2 — SSH into machine

```bash
cfleet machine ssh gcp-box
```

**Verify**:
- [ ] Opens interactive SSH session
- [ ] `claude --version` works inside the VM
- [ ] `python3 -c "import fastapi; print('ok')"` works
- [ ] `/opt/cfleet-relay/worker_relay.py` exists
- [ ] Exit the SSH session

### 1.3 — Verify bootstrap artifacts

```bash
# From inside SSH:
cat ~/.cfleet-env          # should have ANTHROPIC_API_KEY + GITHUB_TOKEN
cat ~/.claude/settings.json  # should have allowedTools permissions
ls /opt/cfleet-relay/       # should have worker_relay.py
```

**Verify**:
- [ ] `~/.cfleet-env` contains both `ANTHROPIC_API_KEY` and `GITHUB_TOKEN`
- [ ] Claude Code settings are pre-configured
- [ ] Relay script is deployed

---

## 2. GCP — Worker Lifecycle

### 2.1 — Spawn worker on existing machine

```bash
cfleet spawn gcp-agent-1 --machine gcp-box
```

**Verify**:
- [ ] Worker provisioning completes (workspace setup, repo clone, systemd service)
- [ ] `cfleet ls` shows `gcp-agent-1` with status `idle` and machine `gcp-box`
- [ ] Server shows worker as connected (`curl http://127.0.0.1:8420/api/server/info` → `connected_workers` contains `gcp-agent-1`)

### 2.2 — Spawn second worker on same machine

```bash
cfleet spawn gcp-agent-2 --machine gcp-box --model claude-sonnet-4-6
```

**Verify**:
- [ ] Second worker provisions on the same VM
- [ ] `cfleet ls` shows both `gcp-agent-1` and `gcp-agent-2` on `gcp-box`
- [ ] `cfleet machine ls` shows `gcp-box` with 2 workers
- [ ] Both workers show as connected to server

### 2.3 — Verify worker isolation (SSH in)

```bash
cfleet machine ssh gcp-box
# Inside VM:
ls /workspace/              # should have gcp-agent-1/ and gcp-agent-2/
systemctl status cfleet-relay-gcp-agent-1
systemctl status cfleet-relay-gcp-agent-2
curl http://127.0.0.1:8421/health   # agent-1 relay
curl http://127.0.0.1:8422/health   # agent-2 relay
ls /workspace/gcp-agent-1/repos/    # should have claude-fleet clone
```

**Verify**:
- [ ] Each worker has its own `/workspace/{name}/` directory
- [ ] Each worker has its own systemd service on a unique port (8421, 8422)
- [ ] Both relays respond to health checks
- [ ] Repos are cloned with push disabled

### 2.4 — Worker status

```bash
cfleet status gcp-agent-1
```

**Verify**:
- [ ] Shows worker name, machine, model, status (idle), relay alive, connected
- [ ] Shows machine IP and provider

---

## 3. GCP — Command Routing (Server -> WebSocket -> Worker)

### 3.1 — Ask a prompt

```bash
cfleet ask gcp-agent-1 "What files are in the current directory? List them briefly."
```

**Verify**:
- [ ] Command returns immediately (or shows working status)
- [ ] `cfleet status gcp-agent-1` transitions to `working` then back to `idle`

### 3.2 — Stream logs

```bash
cfleet logs gcp-agent-1
```

**Verify**:
- [ ] Shows the conversation: user message + assistant response
- [ ] Response references actual files in `/workspace/gcp-agent-1/`

### 3.3 — Ask with follow-up (conversation continuity)

```bash
cfleet ask gcp-agent-1 "Now create a file called hello.txt with 'Hello from fleet' in it."
```

**Verify**:
- [ ] Agent creates the file
- [ ] `cfleet logs gcp-agent-1` shows both conversations
- [ ] SSH in and verify: `cat /workspace/gcp-agent-1/hello.txt`

### 3.4 — Ask second worker (parallel agents)

```bash
cfleet ask gcp-agent-2 "What model are you? Reply in one sentence."
```

**Verify**:
- [ ] Response mentions sonnet (since we set `--model claude-sonnet-4-6`)
- [ ] `cfleet logs gcp-agent-2` shows this conversation
- [ ] `cfleet logs gcp-agent-1` is unaffected (separate conversation)

### 3.5 — Interrupt

```bash
cfleet ask gcp-agent-1 "Count from 1 to 1000, one number per line."
# Immediately:
cfleet interrupt gcp-agent-1
```

**Verify**:
- [ ] Interrupt returns success
- [ ] Agent stops (doesn't reach 1000)
- [ ] `cfleet status gcp-agent-1` shows `idle` after a moment

### 3.6 — Verify secrets are available to agent

```bash
cfleet ask gcp-agent-1 'Run: echo $GITHUB_TOKEN | head -c 4'
```

**Verify**:
- [ ] Agent outputs `ghp_` (first 4 chars of your GitHub token)
- [ ] This confirms `secrets.env` vars are available in the agent's environment

---

## 4. GCP — File Transfer

### 4.1 — Send files to worker

```bash
echo "test data" > /tmp/test-send.txt
cfleet send gcp-agent-1 /tmp/test-send.txt
```

**Verify**:
- [ ] File is copied to `/workspace/gcp-agent-1/inbox/test-send.txt` on the VM
- [ ] SSH in and verify: `cat /workspace/gcp-agent-1/inbox/test-send.txt`

### 4.2 — Collect files from worker

```bash
# First, have the agent create something in outbox
cfleet ask gcp-agent-1 "Create a file at /workspace/gcp-agent-1/outbox/result.txt with 'task complete'"
# Then collect
cfleet collect gcp-agent-1 /tmp/collected/
```

**Verify**:
- [ ] `/tmp/collected/result.txt` exists locally with content "task complete"

### 4.3 — Attach (interactive SSH to worker's machine)

```bash
cfleet attach gcp-agent-1
```

**Verify**:
- [ ] Opens interactive SSH session to gcp-box
- [ ] You're in the worker's workspace directory
- [ ] Exit cleanly

---

## 5. GCP — Cleanup

### 5.1 — Kill single worker

```bash
cfleet kill gcp-agent-2
```

**Verify**:
- [ ] `cfleet ls` shows only `gcp-agent-1`
- [ ] `cfleet machine ls` shows `gcp-box` with 1 worker
- [ ] Server no longer lists `gcp-agent-2` as connected
- [ ] SSH in: `systemctl status cfleet-relay-gcp-agent-2` → inactive/not found

### 5.2 — Kill worker + remove machine

```bash
cfleet kill gcp-agent-1 --remove-machine
```

**Verify**:
- [ ] Worker removed from state
- [ ] Machine removed from state
- [ ] `cfleet ls` is empty
- [ ] `cfleet machine ls` is empty
- [ ] GCP console: VM is deleted (Pulumi destroyed it)

---

## 6. Azure — Full Cycle

### 6.1 — Switch provider or create Azure machine directly

```bash
cfleet machine create az-box --provider azure --region eastus
```

**Verify**:
- [ ] Pulumi provisions an Azure VM
- [ ] `cfleet machine ls` shows `az-box` with status `ready` and an IP
- [ ] Azure portal shows the VM running

### 6.2 — Spawn + ask + logs

```bash
cfleet spawn az-agent --machine az-box
cfleet ask az-agent "Hello from Azure. What's your working directory?"
cfleet logs az-agent
```

**Verify**:
- [ ] Worker spawns successfully
- [ ] Response references `/workspace/az-agent/`
- [ ] Logs show the full conversation

### 6.3 — Confidential VM (SNP) — optional

```bash
cfleet machine create az-snp --provider azure --type snp --region eastus
cfleet spawn snp-agent --machine az-snp
cfleet ask snp-agent "Run: dmesg | grep -i sev"
```

**Verify**:
- [ ] VM is created with AMD SEV-SNP enabled
- [ ] Agent's dmesg output mentions SEV/SNP

### 6.4 — Cleanup Azure

```bash
cfleet kill az-agent --remove-machine
# If az-snp was created:
cfleet kill snp-agent --remove-machine
```

**Verify**:
- [ ] All Azure resources cleaned up
- [ ] `cfleet ls` and `cfleet machine ls` are empty

---

## 7. Auto-Create Machine (spawn without --machine)

### 7.1 — Spawn with auto-machine (GCP)

```bash
cfleet spawn auto-agent --provider gcp
```

**Verify**:
- [ ] Auto-creates a machine named `machine-auto-agent`
- [ ] Machine bootstrapped and worker provisioned
- [ ] `cfleet machine ls` shows the auto-created machine
- [ ] `cfleet ls` shows `auto-agent` on `machine-auto-agent`

### 7.2 — Cleanup

```bash
cfleet kill auto-agent --remove-machine
```

---

## 8. Graceful Cleanup — Stale Resources

These tests verify that `kill` and `machine rm` never block on unreachable resources.

### 8.1 — Kill VM externally, then purge

```bash
# Create a machine and worker
cfleet machine create doomed --provider gcp
cfleet spawn doomed-agent --machine doomed

# Now kill the VM from GCP console (or gcloud):
gcloud compute instances delete doomed --zone=us-central1-a --quiet

# Fleet state still has the machine + worker:
cfleet ls             # shows doomed-agent
cfleet machine ls     # shows doomed

# Normal kill should warn but succeed:
cfleet kill doomed-agent --remove-machine
```

**Verify**:
- [ ] Kill completes (maybe with warnings about unreachable SSH)
- [ ] `cfleet ls` is empty
- [ ] `cfleet machine ls` is empty
- [ ] No Python tracebacks, no blocking

### 8.2 — Purge flag (skip remote ops entirely)

```bash
# Seed fake state
python3 -c "
import sys; sys.path.insert(0, 'src')
from cfleet.config import FleetState, MachineState, WorkerState
s = FleetState.load()
s.machines['phantom'] = MachineState(name='phantom', provider='gcp', ip='192.0.2.1', status='ready', worker_names=['ghost'], next_relay_port=8422)
s.workers['ghost'] = WorkerState(name='ghost', machine_name='phantom', relay_port=8421, model='claude-opus-4-6', status='idle')
s.save()
"
cfleet ls               # shows ghost
cfleet machine ls        # shows phantom

# Purge — instant removal, no SSH/Pulumi/Docker attempted
cfleet kill ghost --purge
cfleet machine rm phantom --purge
```

**Verify**:
- [ ] Both commands complete instantly (< 1 second)
- [ ] `cfleet ls` is empty
- [ ] `cfleet machine ls` is empty
- [ ] No network calls were attempted

---

## 9. Server API — Direct HTTP

With the server running, test the REST API directly.

```bash
TOKEN=$(grep 'token:' ~/.cfleet/config.yml | tail -1 | awk '{print $2}')

# Spawn a worker for these tests
cfleet machine create api-box --provider gcp
cfleet spawn api-worker --machine api-box
```

### 9.1 — Server info

```bash
curl -s http://127.0.0.1:8420/api/server/info -H "Authorization: Bearer $TOKEN" | jq .
```

**Verify**:
- [ ] Returns `version`, `connected_workers` includes `api-worker`, `connected_count` is 1

### 9.2 — List endpoints

```bash
curl -s http://127.0.0.1:8420/api/workers -H "Authorization: Bearer $TOKEN" | jq .
curl -s http://127.0.0.1:8420/api/machines -H "Authorization: Bearer $TOKEN" | jq .
```

**Verify**:
- [ ] Workers list includes `api-worker` with `connected: true`
- [ ] Machines list includes `api-box`

### 9.3 — Worker detail

```bash
curl -s http://127.0.0.1:8420/api/workers/api-worker -H "Authorization: Bearer $TOKEN" | jq .
```

**Verify**:
- [ ] Shows `name`, `status`, `machine_name`, `relay_port`, `connected: true`, `relay_alive: true`

### 9.4 — Ask via API

```bash
curl -s -X POST http://127.0.0.1:8420/api/workers/api-worker/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Say hello"}' | jq .
```

**Verify**:
- [ ] Returns `ok: true` or `status: working`

### 9.5 — Messages via API

```bash
# Wait a few seconds for the ask to complete
sleep 10
curl -s http://127.0.0.1:8420/api/workers/api-worker/messages -H "Authorization: Bearer $TOKEN" | jq .
```

**Verify**:
- [ ] Returns `messages` array with user + assistant messages

### 9.6 — Auth rejection

```bash
curl -s http://127.0.0.1:8420/api/workers
# Should return 401

curl -s "http://127.0.0.1:8420/api/workers?token=$TOKEN"
# Should return 200 (query param auth)
```

**Verify**:
- [ ] No token → 401
- [ ] Query param token → 200

### 9.7 — Cleanup

```bash
cfleet kill api-worker --remove-machine
```

---

## 10. TUI

```bash
# Spawn a worker first
cfleet spawn tui-test

cfleet tui
```

**Verify**:
- [ ] TUI launches with three panels
- [ ] Worker list shows `tui-test`
- [ ] Press `a` to ask — prompt input appears, type something, agent responds
- [ ] Logs panel streams the conversation
- [ ] Press `i` to interrupt
- [ ] Press `k` to kill (confirm)
- [ ] Press `q` to quit

```bash
# Cleanup if TUI didn't kill
cfleet kill tui-test --remove-machine 2>/dev/null
```

---

## 11. Edge Cases

### 11.1 — Ask nonexistent worker

```bash
cfleet ask nobody "hello"
```

**Verify**: Returns error, doesn't crash

### 11.2 — Double kill

```bash
cfleet spawn temp
cfleet kill temp
cfleet kill temp
```

**Verify**: Second kill says worker not found, doesn't crash

### 11.3 — Kill --all

```bash
cfleet spawn a1
cfleet spawn a2
cfleet kill --all
```

**Verify**:
- [ ] All workers and their machines destroyed
- [ ] `cfleet ls` and `cfleet machine ls` both empty

### 11.4 — Server restart (worker reconnect)

```bash
cfleet machine create reconn-box --provider gcp
cfleet spawn reconn-agent --machine reconn-box

# Kill and restart the server
kill %1  # or however you started cfleet serve
cfleet serve &

# Wait for worker to reconnect (exponential backoff, should be < 60s)
sleep 30
curl -s http://127.0.0.1:8420/api/server/info -H "Authorization: Bearer $TOKEN" | jq .connected_workers
```

**Verify**:
- [ ] `reconn-agent` reappears in connected workers after server restart
- [ ] `cfleet ask reconn-agent "are you there?"` works

```bash
cfleet kill reconn-agent --remove-machine
```

---

## 12. Cost Check

After running tests, verify your cloud spend:

```bash
# GCP
gcloud compute instances list --filter="name~cfleet"
# Should be empty — all VMs destroyed

# Azure
az vm list --query "[?contains(name, 'cfleet')]" -o table
# Should be empty
```

**Verify**:
- [ ] No orphaned VMs in GCP
- [ ] No orphaned VMs in Azure
- [ ] Check Anthropic console for API usage from the test prompts
