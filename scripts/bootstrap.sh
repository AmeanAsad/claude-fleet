#!/usr/bin/env bash
# Bootstrap a fleet worker VM with Claude Code.
# Usage: bash bootstrap.sh <ANTHROPIC_API_KEY>
set -euo pipefail

ANTHROPIC_API_KEY="${1:-}"
if [[ -z "$ANTHROPIC_API_KEY" ]]; then
  echo "Usage: $0 <ANTHROPIC_API_KEY>" >&2
  exit 1
fi

USER="${SUDO_USER:-$(whoami)}"
HOME_DIR="/home/$USER"
WORKSPACE="/workspace"

if [[ "$EUID" -ne 0 ]]; then
  echo "Re-running with sudo..." >&2
  exec sudo bash "$0" "$@"
fi

echo "==> Installing system packages"
apt-get install -y software-properties-common 2>/dev/null || true
add-apt-repository -y universe 2>/dev/null || true
apt-get update -y
apt-get install -y \
  git curl rsync build-essential screen \
  ripgrep fd-find jq wget unzip \
  libssl-dev pkg-config ncurses-bin

echo "==> Installing Claude Code"
if [[ ! -f "$HOME_DIR/.local/bin/claude" ]]; then
  sudo -u "$USER" bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
fi

echo "==> Configuring Claude Code"
install -d -o "$USER" -m 0700 "$HOME_DIR/.claude"

# API key file
install -o "$USER" -m 0600 /dev/null "$HOME_DIR/.claude/.api-key"
echo -n "$ANTHROPIC_API_KEY" > "$HOME_DIR/.claude/.api-key"

# settings.json
cat > "$HOME_DIR/.claude/settings.json" <<EOF
{
  "model": "claude-opus-4-6",
  "permissions": {
    "allow": ["Bash(*)", "Read(*)", "Write(*)", "Edit(*)", "Glob(*)", "Grep(*)", "Agent(*)"],
    "deny": []
  },
  "skipDangerousModePermissionPrompt": true
}
EOF
chown "$USER" "$HOME_DIR/.claude/settings.json"
chmod 0600 "$HOME_DIR/.claude/settings.json"

# Pre-accept onboarding
cat > "$HOME_DIR/.claude.json" <<EOF
{
  "hasCompletedOnboarding": true,
  "hasAcknowledgedDisclaimer": true,
  "effortCalloutDismissed": true,
  "projects": {
    "/workspace": {
      "hasTrustDialogAccepted": true,
      "allowedTools": []
    },
    "$HOME_DIR": {
      "hasTrustDialogAccepted": true,
      "allowedTools": []
    }
  }
}
EOF
chown "$USER" "$HOME_DIR/.claude.json"
chmod 0600 "$HOME_DIR/.claude.json"

cat > "$HOME_DIR/.claude/claude.json" <<EOF
{
  "hasCompletedOnboarding": true,
  "hasTrustDialogAccepted": true,
  "hasTrustDialogHooksAccepted": true,
  "hasCompletedProjectOnboarding": true
}
EOF
chown "$USER" "$HOME_DIR/.claude/claude.json"
chmod 0600 "$HOME_DIR/.claude/claude.json"

# bashrc exports
grep -qF 'ANTHROPIC_API_KEY' "$HOME_DIR/.bashrc" || \
  echo "export ANTHROPIC_API_KEY=\"$ANTHROPIC_API_KEY\"" >> "$HOME_DIR/.bashrc"
grep -qF 'CLAUDE_CODE_API_KEY' "$HOME_DIR/.bashrc" || \
  echo "export CLAUDE_CODE_API_KEY=\"$ANTHROPIC_API_KEY\"" >> "$HOME_DIR/.bashrc"
grep -qF '.local/bin' "$HOME_DIR/.bashrc" || \
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME_DIR/.bashrc"

echo "==> Creating workspace"
install -d -o "$USER" -g "$USER" -m 0755 "$WORKSPACE"
sudo -u "$USER" bash -c "cd $WORKSPACE && git init -q"

echo "==> Starting screen session"
sudo -u "$USER" bash -c "screen -dmS main bash" || true

echo ""
echo "Done."
echo "  Claude Code: $("$HOME_DIR/.local/bin/claude" --version 2>/dev/null || echo 'check PATH')"
echo "  screen -r main"
