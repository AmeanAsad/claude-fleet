#!/usr/bin/env bash
set -euo pipefail

# Claude Fleet — central server install script
# Usage: curl -sSL <url>/install-server.sh | bash
#    or: bash install-server.sh [--repo-dir /path/to/claude-fleet]
#
# What this does:
#   1. Installs system deps (python3, pip, git, docker)
#   2. Clones the repo (or uses an existing checkout)
#   3. pip-installs cfleet
#   4. Runs cfleet init (non-interactive with defaults)
#   5. Builds the web dashboard
#   6. Creates a systemd service for cfleet serve
#   7. Starts the server and prints the token

REPO_URL="https://github.com/ameanasad/claude-fleet.git"
BRANCH="v1-cleanup"
INSTALL_DIR="${CFLEET_INSTALL_DIR:-$HOME/claude-fleet}"
FLEET_PORT="${CFLEET_PORT:-8420}"
FLEET_HOST="${CFLEET_HOST:-0.0.0.0}"

RED='\033[0;31m'
GREEN='\033[0;32m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

info()  { echo -e "${GREEN}==>${RESET} $*"; }
dim()   { echo -e "${DIM}    $*${RESET}"; }
err()   { echo -e "${RED}ERROR:${RESET} $*" >&2; }

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
REPO_DIR=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo-dir) REPO_DIR="$2"; shift 2 ;;
        --port)     FLEET_PORT="$2"; shift 2 ;;
        --host)     FLEET_HOST="$2"; shift 2 ;;
        --branch)   BRANCH="$2"; shift 2 ;;
        *)          err "Unknown flag: $1"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# 1. System dependencies
# ---------------------------------------------------------------------------
info "Installing system dependencies"

if command -v apt-get &>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3 python3-pip python3-venv git curl jq nodejs npm >/dev/null 2>&1
elif command -v dnf &>/dev/null; then
    sudo dnf install -y -q python3 python3-pip git curl jq nodejs npm >/dev/null 2>&1
elif command -v yum &>/dev/null; then
    sudo yum install -y -q python3 python3-pip git curl jq nodejs npm >/dev/null 2>&1
else
    err "No supported package manager found (apt, dnf, yum). Install python3, pip, git, curl, nodejs, npm manually."
    exit 1
fi

# Node.js — need 18+ for Next.js build
NODE_MAJOR=$(node --version 2>/dev/null | sed 's/v//' | cut -d. -f1 || echo 0)
if [[ "$NODE_MAJOR" -lt 18 ]]; then
    info "Installing Node.js 22"
    if command -v apt-get &>/dev/null; then
        curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash - >/dev/null 2>&1
        sudo apt-get install -y -qq nodejs >/dev/null 2>&1
    else
        dim "Node.js 18+ required for web dashboard build. Install manually if needed."
    fi
fi

# Docker (optional, for devcontainer provider)
if ! command -v docker &>/dev/null; then
    info "Installing Docker"
    curl -fsSL https://get.docker.com | sudo sh >/dev/null 2>&1
    sudo usermod -aG docker "$USER" 2>/dev/null || true
    dim "Docker installed. You may need to log out and back in for group membership."
fi

# ---------------------------------------------------------------------------
# 2. Get the repo
# ---------------------------------------------------------------------------
if [[ -n "$REPO_DIR" ]]; then
    INSTALL_DIR="$REPO_DIR"
    info "Using existing repo at $INSTALL_DIR"
else
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        info "Updating existing repo at $INSTALL_DIR"
        git -C "$INSTALL_DIR" fetch origin "$BRANCH" --quiet
        git -C "$INSTALL_DIR" checkout "$BRANCH" --quiet
        git -C "$INSTALL_DIR" pull --quiet
    else
        info "Cloning claude-fleet to $INSTALL_DIR"
        git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$INSTALL_DIR" 2>/dev/null
    fi
fi

# ---------------------------------------------------------------------------
# 3. Install cfleet
# ---------------------------------------------------------------------------
info "Installing cfleet"
pip install --quiet --break-system-packages -e "$INSTALL_DIR" 2>/dev/null \
    || pip install --quiet -e "$INSTALL_DIR"

if ! command -v cfleet &>/dev/null; then
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v cfleet &>/dev/null; then
        err "cfleet not found on PATH after install. Add ~/.local/bin to your PATH."
        exit 1
    fi
fi

dim "$(cfleet --help 2>&1 | head -1)"

# ---------------------------------------------------------------------------
# 4. Initialize config (non-interactive)
# ---------------------------------------------------------------------------
FLEET_DIR="$HOME/.cfleet"

if [[ ! -f "$FLEET_DIR/config.yml" ]]; then
    info "Initializing fleet config at $FLEET_DIR"
    mkdir -p "$FLEET_DIR/skills" "$FLEET_DIR/pulumi-state"

    # Write default config with devcontainer provider
    cat > "$FLEET_DIR/config.yml" <<'YAML'
anthropic_api_key: ""
model: claude-opus-4-6
secrets_env: ~/.cfleet/secrets.env
repos: []
skills_dir: ~/.cfleet/skills/
claude_md: ~/.cfleet/CLAUDE.md
mcp_config: ~/.cfleet/mcp-servers.json
worker_relay_port: 8421
pulumi:
  project: claude-fleet
  stack: default
  backend: "file://~/.cfleet/pulumi-state"
cloud:
  provider: devcontainer
  region: local
  vm_type: regular
  instance_type: docker
  ssh_key: ~/.ssh/id_ed25519
  ssh_user: vscode
  azure:
    subscription_id: ""
    resource_group: ""
    image:
      publisher: Canonical
      offer: ubuntu-24_04-lts
      sku: server
      version: latest
  gcp:
    project_id: ""
    zone: us-central1-a
    image:
      project: ubuntu-os-cloud
      family: ubuntu-2404-lts-amd64
YAML

    # Empty state
    echo '{"machines": {}, "workers": {}}' > "$FLEET_DIR/state.json"

    # Placeholder files
    touch "$FLEET_DIR/secrets.env"
    [[ -f "$FLEET_DIR/CLAUDE.md" ]] || echo "# Fleet Worker Instructions" > "$FLEET_DIR/CLAUDE.md"

    dim "Config written to $FLEET_DIR/config.yml"
    dim "Edit it to set anthropic_api_key and provider settings."
else
    info "Config already exists at $FLEET_DIR/config.yml"
fi

# ---------------------------------------------------------------------------
# 5. Generate server token
# ---------------------------------------------------------------------------
TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

# Inject server block into config if missing
if ! grep -q "^server:" "$FLEET_DIR/config.yml" 2>/dev/null; then
    cat >> "$FLEET_DIR/config.yml" <<YAML
server:
  url: "http://127.0.0.1:${FLEET_PORT}"
  host: "${FLEET_HOST}"
  port: ${FLEET_PORT}
  token: "${TOKEN}"
YAML
    dim "Server token generated and saved to config."
else
    TOKEN=$(python3 -c "
import yaml
with open('$FLEET_DIR/config.yml') as f:
    c = yaml.safe_load(f)
print(c.get('server', {}).get('token', ''))
" 2>/dev/null || echo "")
    if [[ -z "$TOKEN" ]]; then
        err "Could not read server token from config."
        exit 1
    fi
    dim "Using existing server token from config."
fi

# ---------------------------------------------------------------------------
# 6. Build web dashboard
# ---------------------------------------------------------------------------
WEB_DIR="$INSTALL_DIR/web"
if [[ -f "$WEB_DIR/package.json" ]]; then
    info "Building web dashboard"
    (cd "$WEB_DIR" && npm install --silent 2>/dev/null && npm run build 2>/dev/null)
    if [[ -d "$WEB_DIR/out" ]]; then
        dim "Dashboard built at $WEB_DIR/out/"
    else
        dim "Warning: web build did not produce out/ directory."
    fi
else
    dim "No web/package.json found — skipping dashboard build."
fi

# ---------------------------------------------------------------------------
# 7. Create systemd service
# ---------------------------------------------------------------------------
info "Setting up systemd service"

CFLEET_BIN=$(command -v cfleet)
SERVICE_FILE="/etc/systemd/system/cfleet-server.service"

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Claude Fleet Central Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
Group=$(id -gn)
ExecStart=$CFLEET_BIN serve --host $FLEET_HOST --port $FLEET_PORT
WorkingDirectory=$HOME
Restart=on-failure
RestartSec=5
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=$HOME

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable cfleet-server >/dev/null 2>&1
sudo systemctl restart cfleet-server

# Wait for server to come up
for i in $(seq 1 10); do
    if curl -sf "http://127.0.0.1:${FLEET_PORT}/api/server/info" -H "Authorization: Bearer $TOKEN" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}Claude Fleet server is running!${RESET}"
echo ""
echo -e "  Server:     http://${FLEET_HOST}:${FLEET_PORT}"
echo -e "  Dashboard:  http://${FLEET_HOST}:${FLEET_PORT}/"
echo -e "  Token:      ${BOLD}${TOKEN}${RESET}"
echo -e "  Config:     ${FLEET_DIR}/config.yml"
echo -e "  Logs:       journalctl -u cfleet-server -f"
echo ""
echo -e "${DIM}Next steps:${RESET}"
echo -e "  1. Set your Anthropic API key:  ${BOLD}vim ${FLEET_DIR}/config.yml${RESET}"
echo -e "  2. Paste the token into the web dashboard's token field"
echo -e "  3. Spawn a worker:              ${BOLD}cfleet spawn my-worker${RESET}"
echo ""
