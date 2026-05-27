#!/usr/bin/env bash
# Spin up a small Azure VM running cfleet serve.
#
# Provisions an Ubuntu 24.04 VM in your existing fleet resource group, opens
# the SSH and fleet ports, installs cfleet from this branch, and starts the
# server under systemd. Prints the public URL and the auth token at the end —
# point your laptop and your worker VMs at them.
#
# Usage:
#   ./scripts/create-server-vm.sh [--name <name>] [--size <sku>] [--port <n>]
#                                 [--rg <resource-group>] [--branch <git-branch>]
#                                 [--anthropic-key <sk-ant-...>]
#
# Examples:
#   ./scripts/create-server-vm.sh
#   ./scripts/create-server-vm.sh --name fleet-control --size Standard_B2s

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

VM_NAME="cfleet-server"
VM_SIZE="Standard_B2s"           # 2 vCPU / 4 GB — plenty for the server
FLEET_PORT="8420"
RG=""                            # falls back to ~/.cfleet/config.yml :cloud.azure.resource_group
LOCATION=""                      # falls back to config; we'll derive if missing
ADMIN_USER="azureuser"
SSH_KEY="$HOME/.ssh/id_ed25519.pub"
REPO_URL="https://github.com/AmeanAsad/claude-fleet.git"
BRANCH="fleat/v2-fleet"
ANTHROPIC_KEY="${ANTHROPIC_API_KEY:-}"
IMAGE="Canonical:ubuntu-24_04-lts:server:latest"

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)           VM_NAME="$2"; shift 2 ;;
        --size)           VM_SIZE="$2"; shift 2 ;;
        --port)           FLEET_PORT="$2"; shift 2 ;;
        --rg)             RG="$2"; shift 2 ;;
        --location)       LOCATION="$2"; shift 2 ;;
        --branch)         BRANCH="$2"; shift 2 ;;
        --repo)           REPO_URL="$2"; shift 2 ;;
        --anthropic-key)  ANTHROPIC_KEY="$2"; shift 2 ;;
        --ssh-key)        SSH_KEY="$2"; shift 2 ;;
        -h|--help)
            head -n 20 "$0" | tail -n 18 | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Resolve config defaults from ~/.cfleet/config.yml
# ---------------------------------------------------------------------------

CFG="$HOME/.cfleet/config.yml"
if [[ -z "$RG" && -f "$CFG" ]]; then
    RG=$(python3 -c "
import yaml, sys, pathlib
d = yaml.safe_load(pathlib.Path('$CFG').read_text()) or {}
print((d.get('cloud', {}).get('azure', {}).get('resource_group') or '').strip())
" 2>/dev/null || true)
fi
if [[ -z "$LOCATION" && -f "$CFG" ]]; then
    LOCATION=$(python3 -c "
import yaml, sys, pathlib
d = yaml.safe_load(pathlib.Path('$CFG').read_text()) or {}
print((d.get('cloud', {}).get('region') or d.get('cloud', {}).get('azure', {}).get('region') or '').strip())
" 2>/dev/null || true)
fi
[[ -z "$RG" ]] && { echo "ERROR: --rg or cloud.azure.resource_group in ~/.cfleet/config.yml required" >&2; exit 1; }
[[ -z "$LOCATION" ]] && LOCATION="westeurope"

if [[ -z "$ANTHROPIC_KEY" && -f "$CFG" ]]; then
    # Prefer the new secrets.anthropic_api_key; fall back to the legacy top-level field.
    ANTHROPIC_KEY=$(python3 -c "
import yaml, pathlib
d = yaml.safe_load(pathlib.Path('$CFG').read_text()) or {}
sec = (d.get('secrets') or {}).get('anthropic_api_key') or ''
print((sec or d.get('anthropic_api_key') or '').strip())
" 2>/dev/null || true)
fi
[[ -z "$ANTHROPIC_KEY" ]] && {
    echo "ERROR: ANTHROPIC_API_KEY missing — pass --anthropic-key, set the env var," >&2
    echo "       or fill anthropic_api_key in ~/.cfleet/config.yml" >&2
    exit 1
}

# Read GitHub App credentials from the laptop's config (optional but recommended).
# If present, we ship them so the server can mint installation tokens immediately.
GH_APP_ID=""
GH_INSTALL_ID=""
GH_PEM_PATH=""
if [[ -f "$CFG" ]]; then
    eval "$(python3 -c "
import yaml, pathlib, shlex
d = yaml.safe_load(pathlib.Path('$CFG').read_text()) or {}
gh = d.get('github') or {}
print(f'GH_APP_ID={shlex.quote(str(gh.get(\"app_id\") or \"\"))}')
print(f'GH_INSTALL_ID={shlex.quote(str(gh.get(\"installation_id\") or \"\"))}')
print(f'GH_PEM_PATH={shlex.quote(str(gh.get(\"private_key_path\") or \"\"))}')
" 2>/dev/null || true)"
fi
# Expand ~ in the pem path
[[ -n "$GH_PEM_PATH" ]] && GH_PEM_PATH="${GH_PEM_PATH/#\~/$HOME}"

[[ -f "$SSH_KEY" ]] || { echo "ERROR: SSH public key not found at $SSH_KEY" >&2; exit 1; }

az account show >/dev/null 2>&1 || { echo "ERROR: not logged into Azure — run 'az login'" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

GREEN=$'\033[0;32m'; DIM=$'\033[2m'; BOLD=$'\033[1m'; RESET=$'\033[0m'

echo "${BOLD}Provisioning cfleet server VM${RESET}"
echo "  ${DIM}Name:      ${VM_NAME}${RESET}"
echo "  ${DIM}RG:        ${RG}${RESET}"
echo "  ${DIM}Location:  ${LOCATION}${RESET}"
echo "  ${DIM}Size:      ${VM_SIZE}${RESET}"
echo "  ${DIM}Port:      ${FLEET_PORT}${RESET}"
echo "  ${DIM}Branch:    ${BRANCH}${RESET}"
echo

# ---------------------------------------------------------------------------
# Make sure the RG exists
# ---------------------------------------------------------------------------

if ! az group show --name "$RG" >/dev/null 2>&1; then
    echo "${GREEN}==>${RESET} Creating resource group ${BOLD}${RG}${RESET} in ${LOCATION}"
    az group create --name "$RG" --location "$LOCATION" -o none
fi

# ---------------------------------------------------------------------------
# Create the VM (idempotent: skip if it already exists)
# ---------------------------------------------------------------------------

if az vm show -g "$RG" -n "$VM_NAME" >/dev/null 2>&1; then
    echo "${GREEN}==>${RESET} VM ${BOLD}${VM_NAME}${RESET} already exists in ${RG}, reusing it"
else
    echo "${GREEN}==>${RESET} Creating VM ${BOLD}${VM_NAME}${RESET} (~1-2 min)"
    az vm create \
        --resource-group "$RG" \
        --name "$VM_NAME" \
        --image "$IMAGE" \
        --size "$VM_SIZE" \
        --admin-username "$ADMIN_USER" \
        --ssh-key-values "$SSH_KEY" \
        --public-ip-sku Standard \
        --output none
fi

# ---------------------------------------------------------------------------
# Open port 8420 inbound
# ---------------------------------------------------------------------------

echo "${GREEN}==>${RESET} Opening inbound port ${FLEET_PORT}"
NSG_NAME=$(az vm show -g "$RG" -n "$VM_NAME" --query "networkProfile.networkInterfaces[0].id" -o tsv \
    | xargs -I{} az network nic show --ids {} --query "networkSecurityGroup.id" -o tsv)
if [[ -n "$NSG_NAME" ]]; then
    NSG_NAME=$(basename "$NSG_NAME")
    if ! az network nsg rule show -g "$RG" --nsg-name "$NSG_NAME" -n cfleet-server >/dev/null 2>&1; then
        az network nsg rule create \
            --resource-group "$RG" \
            --nsg-name "$NSG_NAME" \
            --name cfleet-server \
            --priority 1010 \
            --destination-port-ranges "$FLEET_PORT" \
            --access Allow \
            --protocol Tcp \
            --output none
    fi
else
    echo "${DIM}    No NSG attached to the VM's NIC — the VM may already allow this port via subnet NSG.${RESET}"
fi

# ---------------------------------------------------------------------------
# Look up the public IP
# ---------------------------------------------------------------------------

PUBLIC_IP=$(az vm show -d -g "$RG" -n "$VM_NAME" --query publicIps -o tsv)
[[ -z "$PUBLIC_IP" ]] && { echo "ERROR: VM has no public IP yet" >&2; exit 1; }
echo "${GREEN}==>${RESET} Public IP: ${BOLD}${PUBLIC_IP}${RESET}"

# ---------------------------------------------------------------------------
# Wait for SSH
# ---------------------------------------------------------------------------

echo "${GREEN}==>${RESET} Waiting for SSH on ${PUBLIC_IP}"
for i in $(seq 1 30); do
    if ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
           -o ConnectTimeout=5 -o LogLevel=ERROR \
           "${ADMIN_USER}@${PUBLIC_IP}" "true" 2>/dev/null; then
        break
    fi
    sleep 5
    [[ $i -eq 30 ]] && { echo "ERROR: SSH never came up" >&2; exit 1; }
done

# ---------------------------------------------------------------------------
# Generate credentials locally so the operator gets them in plaintext exactly
# once. We mint three things:
#   - joiner_token: shared credential machines/workers use to join the fleet
#   - first operator key: identifies this laptop to the operator API
#   - operator key hash: stored on the VM (the raw key is never persisted there)
# ---------------------------------------------------------------------------

JOINER_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
OPERATOR_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
OPERATOR_HASH=$(python3 -c "import hashlib,sys; print(hashlib.sha256('$OPERATOR_KEY'.encode()).hexdigest())")
OPERATOR_NAME="${USER:-laptop}-$(date +%Y%m%d)"
ISSUED_AT=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)

# ---------------------------------------------------------------------------
# Optionally ship the GitHub App private key to the VM
# ---------------------------------------------------------------------------

GH_SECTION=""
if [[ -n "$GH_APP_ID" && -n "$GH_INSTALL_ID" && -f "$GH_PEM_PATH" ]]; then
    echo "${GREEN}==>${RESET} Shipping GitHub App credentials"
    scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        "$GH_PEM_PATH" "${ADMIN_USER}@${PUBLIC_IP}:/tmp/github-app.pem" >/dev/null
    GH_SECTION=$(cat <<GHCFG
github:
  app_id: "${GH_APP_ID}"
  installation_id: "${GH_INSTALL_ID}"
  private_key_path: /root/.cfleet/github-app.pem
GHCFG
)
else
    echo "${DIM}    No GitHub App config in ~/.cfleet/config.yml; server will start without gh broker.${RESET}"
fi

# ---------------------------------------------------------------------------
# Install + run cfleet serve on the VM
# ---------------------------------------------------------------------------

echo "${GREEN}==>${RESET} Installing cfleet on the VM (this takes a couple minutes)"

REMOTE_SCRIPT=$(cat <<REMOTE
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv git curl jq

sudo pip3 install --break-system-packages --quiet "git+${REPO_URL}@${BRANCH}" 2>/dev/null \\
  || sudo pip3 install --quiet "git+${REPO_URL}@${BRANCH}"

CFLEET_BIN="\$(python3 -c 'import shutil; print(shutil.which("cfleet") or "")')"
[ -n "\${CFLEET_BIN}" ] && [ "\${CFLEET_BIN}" != "/usr/local/bin/cfleet" ] && sudo ln -sf "\${CFLEET_BIN}" /usr/local/bin/cfleet

sudo mkdir -p /root/.cfleet
# Move the github-app pem into place if we shipped one
if [ -f /tmp/github-app.pem ]; then
    sudo mv /tmp/github-app.pem /root/.cfleet/github-app.pem
    sudo chmod 600 /root/.cfleet/github-app.pem
fi

# Write the canonical config. New schema:
#   - secrets.anthropic_api_key  is the canonical Anthropic key
#   - server.joiner_token        is the joiner credential (machines + workers)
#   - server.operator_keys       lists hashed operator keys
#   - server.token               (legacy) is left empty so we don't double-honor anything
sudo tee /root/.cfleet/config.yml >/dev/null <<CFG
model: "claude-opus-4-7"
secrets:
  anthropic_api_key: "${ANTHROPIC_KEY}"
  model: "claude-opus-4-7"
server:
  url: "http://${PUBLIC_IP}:${FLEET_PORT}"
  host: "0.0.0.0"
  port: ${FLEET_PORT}
  joiner_token: "${JOINER_TOKEN}"
  operator_keys:
    - name: "${OPERATOR_NAME}"
      key_hash: "${OPERATOR_HASH}"
      created_at: "${ISSUED_AT}"
${GH_SECTION}
CFG
sudo chmod 600 /root/.cfleet/config.yml

sudo tee /etc/systemd/system/cfleet-server.service >/dev/null <<UNIT
[Unit]
Description=Claude Fleet central server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=HOME=/root
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/local/bin/cfleet serve --host 0.0.0.0 --port ${FLEET_PORT}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now cfleet-server.service
sleep 3
sudo systemctl status cfleet-server.service --no-pager | head -10
REMOTE
)

ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "${ADMIN_USER}@${PUBLIC_IP}" "bash -s" <<<"$REMOTE_SCRIPT"

# ---------------------------------------------------------------------------
# Done — print the connect info
# ---------------------------------------------------------------------------

SERVER_URL="http://${PUBLIC_IP}:${FLEET_PORT}"

cat <<DONE

${GREEN}${BOLD}cfleet server is up.${RESET}

  URL:           ${BOLD}${SERVER_URL}${RESET}
  Operator key:  ${BOLD}${OPERATOR_KEY}${RESET}    (name: ${OPERATOR_NAME})
  Joiner token:  ${BOLD}${JOINER_TOKEN}${RESET}

${DIM}Save the operator key now — the server only stores its hash and can't show it again.${RESET}

${BOLD}Connect this laptop (operator mode):${RESET}
  cfleet connect ${SERVER_URL} --operator-key ${OPERATOR_KEY}

${BOLD}Issue an operator key for a second device:${RESET}
  cfleet operator add <name>

${BOLD}Join a worker host:${RESET}
  cfleet join ${SERVER_URL} --joiner-token ${JOINER_TOKEN}

${BOLD}Tail the server logs:${RESET}
  ssh ${ADMIN_USER}@${PUBLIC_IP} 'sudo journalctl -u cfleet-server -f'

${BOLD}Tear down:${RESET}
  az vm delete -g ${RG} -n ${VM_NAME} --yes

DONE
