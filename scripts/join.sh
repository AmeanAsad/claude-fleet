#!/usr/bin/env bash
set -euo pipefail

# Claude Fleet — join an existing fleet from any machine.
# Usage: curl -sSL <url>/join.sh | bash -s -- http://server:8420 --token TOKEN
#    or: bash join.sh http://server:8420 --token TOKEN [--name my-box] [--api-key sk-ant-...]

RED='\033[0;31m'
GREEN='\033[0;32m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

info()  { echo -e "${GREEN}==>${RESET} $*"; }
dim()   { echo -e "${DIM}    $*${RESET}"; }
err()   { echo -e "${RED}ERROR:${RESET} $*" >&2; }

SERVER_URL=""
TOKEN=""
NAME=""
API_KEY=""
SKIP_BOOTSTRAP=""
MODEL="claude-opus-4-7"
REPO_URL="https://github.com/ameanasad/claude-fleet.git"
BRANCH="fleat/v2-fleet"

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        http://*|https://*)
            SERVER_URL="$1"; shift ;;
        --token)       TOKEN="$2"; shift 2 ;;
        --name)        NAME="$2"; shift 2 ;;
        --api-key)     API_KEY="$2"; shift 2 ;;
        --model)       MODEL="$2"; shift 2 ;;
        --skip-bootstrap) SKIP_BOOTSTRAP="--skip-bootstrap"; shift ;;
        *)             err "Unknown arg: $1"; exit 1 ;;
    esac
done

if [[ -z "$SERVER_URL" ]]; then
    err "Usage: join.sh <server-url> --token <token> [--name <name>] [--api-key <key>]"
    exit 1
fi

if [[ -z "$TOKEN" ]]; then
    err "Missing --token. Get it from the fleet server operator."
    exit 1
fi

# Install system deps
info "Installing prerequisites"
if command -v apt-get &>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3 python3-pip python3-venv git curl >/dev/null 2>&1
elif command -v dnf &>/dev/null; then
    sudo dnf install -y -q python3 python3-pip git curl >/dev/null 2>&1
elif command -v yum &>/dev/null; then
    sudo yum install -y -q python3 python3-pip git curl >/dev/null 2>&1
else
    err "No supported package manager found. Install python3, pip, git manually."
    exit 1
fi

# Install cfleet
if ! command -v cfleet &>/dev/null; then
    info "Installing cfleet"
    INSTALL_DIR="${CFLEET_INSTALL_DIR:-$HOME/claude-fleet}"
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        git -C "$INSTALL_DIR" fetch origin "$BRANCH" --quiet
        git -C "$INSTALL_DIR" checkout "$BRANCH" --quiet 2>/dev/null || true
        git -C "$INSTALL_DIR" pull --quiet 2>/dev/null || true
    else
        git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$INSTALL_DIR" 2>/dev/null
    fi
    pip install --quiet --break-system-packages -e "$INSTALL_DIR" 2>/dev/null \
        || pip install --quiet -e "$INSTALL_DIR"
    export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v cfleet &>/dev/null; then
    err "cfleet not found after install. Add ~/.local/bin to your PATH."
    exit 1
fi

# Build join command
CMD="cfleet join ${SERVER_URL} --token ${TOKEN} --model ${MODEL}"
[[ -n "$NAME" ]] && CMD="$CMD --name $NAME"
[[ -n "$API_KEY" ]] && CMD="$CMD --api-key $API_KEY"
[[ -n "$SKIP_BOOTSTRAP" ]] && CMD="$CMD --skip-bootstrap"

echo ""
echo -e "${GREEN}${BOLD}Ready to join the fleet!${RESET}"
echo -e "  Server:  ${SERVER_URL}"
echo -e "  Machine: ${NAME:-$(hostname)}"
echo ""

exec $CMD
