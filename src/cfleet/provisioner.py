"""Bash-based provisioning — replaces Ansible for machine bootstrap and worker setup."""

from __future__ import annotations

import json
import shlex
import textwrap
from pathlib import Path

from cfleet.ssh import rsync_to, ssh_run, ssh_run_script


class ProvisionError(Exception):
    pass


def _bootstrap_machine_sh() -> str:
    """Return the bash script that bootstraps a machine (run once per machine)."""
    return textwrap.dedent("""\
        #!/usr/bin/env bash
        set -euo pipefail

        echo "==> Installing system packages..."
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -y
        apt-get install -y \\
            git curl rsync build-essential ripgrep jq wget unzip \\
            libssl-dev pkg-config python3-pip fd-find ncurses-bin

        echo "==> Installing Claude Code CLI..."
        if [ ! -f "/home/${CFLEET_SSH_USER}/.local/bin/claude" ]; then
            su - "${CFLEET_SSH_USER}" -c 'curl -fsSL https://claude.ai/install.sh | bash'
        fi

        echo "==> Configuring Claude Code..."
        CLAUDE_DIR="/home/${CFLEET_SSH_USER}/.claude"
        mkdir -p "${CLAUDE_DIR}"
        chown "${CFLEET_SSH_USER}:${CFLEET_SSH_USER}" "${CLAUDE_DIR}"
        chmod 700 "${CLAUDE_DIR}"

        echo -n "${CFLEET_API_KEY}" > "${CLAUDE_DIR}/.api-key"
        chown "${CFLEET_SSH_USER}:${CFLEET_SSH_USER}" "${CLAUDE_DIR}/.api-key"
        chmod 600 "${CLAUDE_DIR}/.api-key"

        cat > "${CLAUDE_DIR}/settings.json" << SETTINGS_EOF
        {
          "model": "${CFLEET_MODEL}",
          "alwaysThinkingEnabled": true,
          "skipDangerousModePermissionPrompt": true,
          "apiKeyHelper": "cat /home/${CFLEET_SSH_USER}/.claude/.api-key",
          "effortLevel": "high",
          "permissions": {
            "allow": ["Read", "Write", "Edit", "MultiEdit", "Bash(*)", "WebFetch"]
          }
        }
        SETTINGS_EOF
        chown "${CFLEET_SSH_USER}:${CFLEET_SSH_USER}" "${CLAUDE_DIR}/settings.json"
        chmod 600 "${CLAUDE_DIR}/settings.json"

        cat > "/home/${CFLEET_SSH_USER}/.claude.json" << ONBOARD_EOF
        {
          "hasCompletedOnboarding": true,
          "hasAcknowledgedDisclaimer": true,
          "effortCalloutDismissed": true,
          "projects": {
            "/workspace": {"hasTrustDialogAccepted": true, "allowedTools": []},
            "/home/${CFLEET_SSH_USER}": {"hasTrustDialogAccepted": true, "allowedTools": []}
          }
        }
        ONBOARD_EOF
        chown "${CFLEET_SSH_USER}:${CFLEET_SSH_USER}" "/home/${CFLEET_SSH_USER}/.claude.json"

        cat > "${CLAUDE_DIR}/claude.json" << TRUST_EOF
        {
          "hasCompletedOnboarding": true,
          "hasTrustDialogAccepted": true,
          "hasTrustDialogHooksAccepted": true,
          "hasCompletedProjectOnboarding": true
        }
        TRUST_EOF
        chown "${CFLEET_SSH_USER}:${CFLEET_SSH_USER}" "${CLAUDE_DIR}/claude.json"

        grep -q 'CLAUDE_CODE_API_KEY' "/home/${CFLEET_SSH_USER}/.bashrc" 2>/dev/null || \\
            echo "export CLAUDE_CODE_API_KEY=\\"${CFLEET_API_KEY}\\"" >> "/home/${CFLEET_SSH_USER}/.bashrc"
        grep -q '/.local/bin' "/home/${CFLEET_SSH_USER}/.bashrc" 2>/dev/null || \\
            echo 'export PATH="/home/'"${CFLEET_SSH_USER}"'/.local/bin:$PATH"' >> "/home/${CFLEET_SSH_USER}/.bashrc"

        echo "==> Installing relay Python dependencies..."
        pip3 install --break-system-packages --quiet \\
            claude-code-sdk httpx fastapi uvicorn sse-starlette pydantic PyJWT cryptography 2>/dev/null || \\
        pip3 install --quiet \\
            claude-code-sdk httpx fastapi uvicorn sse-starlette pydantic PyJWT cryptography

        echo "==> Deploying relay and credential helper..."
        mkdir -p /opt/cfleet-relay
        for f in worker_relay.py credential_helper.py; do
            [ -f "/tmp/cfleet-staging/${f}" ] && cp "/tmp/cfleet-staging/${f}" "/opt/cfleet-relay/${f}"
        done
        chown -R "${CFLEET_SSH_USER}:${CFLEET_SSH_USER}" /opt/cfleet-relay

        ln -sf /opt/cfleet-relay/credential_helper.py /usr/local/bin/cfleet-gh-token
        chmod +x /opt/cfleet-relay/credential_helper.py

        echo "==> Machine bootstrap complete."
    """)


def _provision_worker_sh() -> str:
    """Return the bash script that provisions a single worker on a machine."""
    return textwrap.dedent("""\
        #!/usr/bin/env bash
        set -euo pipefail

        WORKER_DIR="${CFLEET_WORKSPACE}/${CFLEET_WORKER_NAME}"
        STAGING="/tmp/cfleet-staging-${CFLEET_WORKER_NAME}"
        HOME_DIR="/home/${CFLEET_SSH_USER}"
        GH_LEVEL="${CFLEET_GH_LEVEL:-none}"

        echo "==> Setting up workspace for ${CFLEET_WORKER_NAME}..."
        mkdir -p "${WORKER_DIR}"/{repos,inbox,outbox}
        chown -R "${CFLEET_SSH_USER}:${CFLEET_SSH_USER}" "${WORKER_DIR}"

        if [ ! -d "${WORKER_DIR}/.git" ]; then
            su - "${CFLEET_SSH_USER}" -c "cd ${WORKER_DIR} && git init"
        fi

        # Clone repos
        if [ -n "${CFLEET_REPOS_JSON:-}" ] && [ "${CFLEET_REPOS_JSON}" != "[]" ]; then
            echo "==> Cloning repos..."
            echo "${CFLEET_REPOS_JSON}" | python3 -c "
        import json, subprocess, sys, os
        repos = json.load(sys.stdin)
        user = os.environ['CFLEET_SSH_USER']
        worker_dir = os.environ.get('WORKER_DIR', '${WORKER_DIR}')
        gh_level = os.environ.get('CFLEET_GH_LEVEL', 'none')
        for r in repos:
            dest = f'{worker_dir}/repos/{r[\"name\"]}'
            if os.path.exists(dest):
                print(f'  Repo {r[\"name\"]} already cloned, skipping')
                continue
            branch = r.get('branch', 'main')
            subprocess.run(['su', '-', user, '-c',
                f'git clone --depth 1 --single-branch -b {branch} {r[\"url\"]} {dest}'],
                check=True)
            if gh_level != 'write':
                subprocess.run(['su', '-', user, '-c',
                    f'cd {dest} && git remote set-url --push origin no_push_allowed'],
                    check=True)
                hook = f'{dest}/.git/hooks/pre-push'
                with open(hook, 'w') as f:
                    f.write('#!/bin/sh\\necho \"Push disabled (gh_level != write)\"\\nexit 1\\n')
                os.chmod(hook, 0o755)
            print(f'  Cloned {r[\"name\"]} ({branch}, push={\"enabled\" if gh_level == \"write\" else \"disabled\"})')
        "
        fi

        # Deploy files from staging directory
        if [ -d "${STAGING}" ]; then
            echo "==> Deploying worker config files..."
            [ -d "${STAGING}/skills" ] && \\
                cp -r "${STAGING}/skills" "${HOME_DIR}/.claude/skills/" 2>/dev/null || true
            [ -f "${STAGING}/CLAUDE.md" ] && \\
                cp "${STAGING}/CLAUDE.md" "${WORKER_DIR}/CLAUDE.md" 2>/dev/null || true
            [ -f "${STAGING}/mcp-servers.json" ] && \\
                cp "${STAGING}/mcp-servers.json" "${HOME_DIR}/.claude/mcp-servers.json" 2>/dev/null || true
            [ -f "${STAGING}/secrets.env" ] && {
                cp "${STAGING}/secrets.env" "${HOME_DIR}/.cfleet-env"
                chmod 600 "${HOME_DIR}/.cfleet-env"
                grep -q 'cfleet-env' "${HOME_DIR}/.bashrc" 2>/dev/null || \\
                    echo '[ -f ~/.cfleet-env ] && source ~/.cfleet-env' >> "${HOME_DIR}/.bashrc"
            } 2>/dev/null || true
            chown -R "${CFLEET_SSH_USER}:${CFLEET_SSH_USER}" "${HOME_DIR}/.claude" 2>/dev/null || true
            rm -rf "${STAGING}"
        fi

        # Configure git credential helper (uses fleet server for GitHub tokens)
        if [ -f /opt/cfleet-relay/credential_helper.py ] && [ "${GH_LEVEL}" != "none" ]; then
            echo "==> Configuring git credential helper..."
            su - "${CFLEET_SSH_USER}" -c "git config --global credential.https://github.com.helper '/usr/local/bin/cfleet-gh-token'"
            su - "${CFLEET_SSH_USER}" -c "git config --global credential.https://github.com.useHttpPath true"
        fi

        # Build systemd EnvironmentFile from secrets.env (fixes the systemd env gap)
        ENVFILE="/etc/cfleet/${CFLEET_WORKER_NAME}.env"
        mkdir -p /etc/cfleet
        cat > "${ENVFILE}" << ENVEOF
        ANTHROPIC_API_KEY=${CFLEET_API_KEY}
        CLAUDE_CODE_API_KEY=${CFLEET_API_KEY}
        CFLEET_MODEL=${CFLEET_MODEL}
        CFLEET_SERVER_URL=${CFLEET_SERVER_URL:-}
        CFLEET_TOKEN=${CFLEET_TOKEN:-}
        CFLEET_WORKER_NAME=${CFLEET_WORKER_NAME}
        CFLEET_MACHINE_NAME=${CFLEET_MACHINE_NAME:-}
        CFLEET_GH_LEVEL=${GH_LEVEL}
        ENVEOF
        # Append user secrets (GITHUB_TOKEN etc.) so the relay process has them
        [ -f "${HOME_DIR}/.cfleet-env" ] && grep -v '^#' "${HOME_DIR}/.cfleet-env" | grep '=' >> "${ENVFILE}" || true
        chmod 600 "${ENVFILE}"

        echo "==> Creating systemd service cfleet-relay-${CFLEET_WORKER_NAME}..."
        cat > "/etc/systemd/system/cfleet-relay-${CFLEET_WORKER_NAME}.service" << SVC_EOF
        [Unit]
        Description=cfleet relay for ${CFLEET_WORKER_NAME}
        After=network.target

        [Service]
        Type=simple
        User=${CFLEET_SSH_USER}
        WorkingDirectory=${WORKER_DIR}
        EnvironmentFile=${ENVFILE}
        Environment=PATH=${HOME_DIR}/.local/bin:/usr/local/bin:/usr/bin:/bin
        ExecStart=/usr/bin/python3 /opt/cfleet-relay/worker_relay.py --port ${CFLEET_RELAY_PORT} --host 127.0.0.1 --model ${CFLEET_MODEL} --cwd ${WORKER_DIR} --server-url=${CFLEET_SERVER_URL:-} --token=${CFLEET_TOKEN:-} --worker-name ${CFLEET_WORKER_NAME} --machine-name ${CFLEET_MACHINE_NAME:-}
        Restart=on-failure
        RestartSec=5

        [Install]
        WantedBy=multi-user.target
        SVC_EOF

        systemctl daemon-reload
        systemctl enable --now "cfleet-relay-${CFLEET_WORKER_NAME}.service"

        echo "==> Waiting for relay health check on port ${CFLEET_RELAY_PORT}..."
        for i in $(seq 1 20); do
            if curl -sf "http://127.0.0.1:${CFLEET_RELAY_PORT}/health" > /dev/null 2>&1; then
                echo "==> Worker ${CFLEET_WORKER_NAME} is ready (port ${CFLEET_RELAY_PORT})."
                exit 0
            fi
            sleep 2
        done
        echo "ERROR: Relay failed to start within 40 seconds"
        exit 1
    """)


def _stage_files(
    ip: str,
    user: str,
    key_path: str,
    staging_name: str,
    fleet_config,
) -> None:
    """rsync local config files to a staging directory on the remote machine."""
    staging_dir = f"/tmp/cfleet-staging-{staging_name}"
    ssh_run(ip, user, key_path, f"mkdir -p {staging_dir}")

    module_dir = Path(__file__).parent
    for script_name in ("worker_relay.py", "credential_helper.py"):
        script = module_dir / script_name
        if script.exists():
            rsync_to(ip, user, key_path, str(script), "/tmp/cfleet-staging")

    skills_dir = fleet_config.resolve_skills_dir()
    if skills_dir.exists():
        rsync_to(ip, user, key_path, str(skills_dir), f"{staging_dir}/skills")

    claude_md = fleet_config.resolve_claude_md()
    if claude_md.exists():
        rsync_to(ip, user, key_path, str(claude_md), staging_dir)

    mcp_config = fleet_config.resolve_mcp_config()
    if mcp_config.exists():
        rsync_to(ip, user, key_path, str(mcp_config), staging_dir)

    secrets_env = fleet_config.resolve_secrets_env()
    if secrets_env.exists():
        rsync_to(ip, user, key_path, str(secrets_env), staging_dir)


def bootstrap_machine(
    ip: str,
    user: str,
    key_path: str,
    fleet_config,
) -> None:
    """Bootstrap a machine: install packages, Claude Code, relay deps."""
    _stage_files(ip, user, key_path, "staging", fleet_config)

    env_vars = {
        "CFLEET_SSH_USER": user,
        "CFLEET_API_KEY": fleet_config.anthropic_api_key,
        "CFLEET_MODEL": fleet_config.model,
    }

    ssh_run_script(ip, user, key_path, _bootstrap_machine_sh(), env_vars=env_vars, timeout=600)


def provision_worker(
    ip: str,
    user: str,
    key_path: str,
    worker_name: str,
    relay_port: int,
    model: str,
    repos: list[dict],
    fleet_config,
    machine_name: str = "",
    workspace: str = "/workspace",
    github_level: str = "none",
) -> None:
    """Provision a single worker on an already-bootstrapped machine."""
    _stage_files(ip, user, key_path, worker_name, fleet_config)

    server_url = ""
    server_token = ""
    if hasattr(fleet_config, "server"):
        if fleet_config.server.url:
            server_url = fleet_config.server.url
        elif fleet_config.server.host and fleet_config.server.port:
            server_url = f"http://{fleet_config.server.host}:{fleet_config.server.port}"
        server_token = fleet_config.server.token

    env_vars = {
        "CFLEET_SSH_USER": user,
        "CFLEET_API_KEY": fleet_config.anthropic_api_key,
        "CFLEET_MODEL": model or fleet_config.model,
        "CFLEET_WORKER_NAME": worker_name,
        "CFLEET_RELAY_PORT": str(relay_port),
        "CFLEET_WORKSPACE": workspace,
        "CFLEET_REPOS_JSON": json.dumps(repos),
        "CFLEET_SERVER_URL": server_url,
        "CFLEET_TOKEN": server_token,
        "CFLEET_MACHINE_NAME": machine_name,
        "CFLEET_GH_LEVEL": github_level,
    }

    ssh_run_script(ip, user, key_path, _provision_worker_sh(), env_vars=env_vars, timeout=600)
