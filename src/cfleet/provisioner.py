"""Bash-based provisioning — replaces Ansible for machine bootstrap and worker setup."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import textwrap
from pathlib import Path

from cfleet.ssh import rsync_to, ssh_run, ssh_run_script


class ProvisionError(Exception):
    pass


def _bootstrap_machine_sh() -> str:
    """Bash script that bootstraps a cloud VM into a fleet machine.

    Installs system deps, Claude Code CLI, cfleet itself from the configured
    GitHub branch, writes ~/.cfleet/config.yml so the machine knows the fleet
    server, and starts the machine-agent daemon as a systemd unit. After this
    runs, the VM is identical to any laptop that did `cfleet join`.
    """
    # SUDO prefix is empty when running as root, "sudo -E" otherwise. We can't
    # rely on cloud-init to leave the script root, but the default ssh user
    # (ubuntu / azureuser / etc.) usually has passwordless sudo.
    return textwrap.dedent("""\
        #!/usr/bin/env bash
        set -euo pipefail

        if [ "$(id -u)" -ne 0 ]; then
            SUDO="sudo -E"
        else
            SUDO=""
        fi

        echo "==> Installing system packages..."
        export DEBIAN_FRONTEND=noninteractive
        ${SUDO} apt-get update -y
        ${SUDO} apt-get install -y \\
            git curl rsync build-essential ripgrep jq wget unzip \\
            libssl-dev pkg-config python3-pip python3-venv \\
            fd-find ncurses-bin

        echo "==> Installing Claude Code CLI..."
        if [ ! -f "/home/${CFLEET_SSH_USER}/.local/bin/claude" ]; then
            ${SUDO} -u "${CFLEET_SSH_USER}" bash -lc 'curl -fsSL https://claude.ai/install.sh | bash'
        fi

        echo "==> Configuring Claude Code..."
        CLAUDE_DIR="/home/${CFLEET_SSH_USER}/.claude"
        ${SUDO} mkdir -p "${CLAUDE_DIR}"
        ${SUDO} chown "${CFLEET_SSH_USER}:${CFLEET_SSH_USER}" "${CLAUDE_DIR}"
        ${SUDO} chmod 700 "${CLAUDE_DIR}"

        echo -n "${CFLEET_API_KEY}" | ${SUDO} tee "${CLAUDE_DIR}/.api-key" >/dev/null
        ${SUDO} chown "${CFLEET_SSH_USER}:${CFLEET_SSH_USER}" "${CLAUDE_DIR}/.api-key"
        ${SUDO} chmod 600 "${CLAUDE_DIR}/.api-key"

        ${SUDO} tee "${CLAUDE_DIR}/settings.json" >/dev/null << SETTINGS_EOF
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
        ${SUDO} chown "${CFLEET_SSH_USER}:${CFLEET_SSH_USER}" "${CLAUDE_DIR}/settings.json"
        ${SUDO} chmod 600 "${CLAUDE_DIR}/settings.json"

        ${SUDO} tee "/home/${CFLEET_SSH_USER}/.claude.json" >/dev/null << ONBOARD_EOF
        {
          "hasCompletedOnboarding": true,
          "hasAcknowledgedDisclaimer": true,
          "effortCalloutDismissed": true,
          "projects": {
            "/home/${CFLEET_SSH_USER}": {"hasTrustDialogAccepted": true, "allowedTools": []}
          }
        }
        ONBOARD_EOF
        ${SUDO} chown "${CFLEET_SSH_USER}:${CFLEET_SSH_USER}" "/home/${CFLEET_SSH_USER}/.claude.json"

        if ! grep -q 'CLAUDE_CODE_API_KEY' "/home/${CFLEET_SSH_USER}/.bashrc" 2>/dev/null; then
            echo "export CLAUDE_CODE_API_KEY=\\"${CFLEET_API_KEY}\\"" | ${SUDO} tee -a "/home/${CFLEET_SSH_USER}/.bashrc" >/dev/null
        fi
        if ! grep -q '/.local/bin' "/home/${CFLEET_SSH_USER}/.bashrc" 2>/dev/null; then
            echo 'export PATH="/home/'"${CFLEET_SSH_USER}"'/.local/bin:$PATH"' | ${SUDO} tee -a "/home/${CFLEET_SSH_USER}/.bashrc" >/dev/null
        fi

        echo "==> Installing cfleet from ${CFLEET_REPO}@${CFLEET_BRANCH}..."
        ${SUDO} pip3 install --break-system-packages --quiet \\
            "git+${CFLEET_REPO}@${CFLEET_BRANCH}" 2>/dev/null || \\
        ${SUDO} pip3 install --quiet "git+${CFLEET_REPO}@${CFLEET_BRANCH}"

        # Make sure both cfleet entrypoints exist on PATH for all users.
        CFLEET_BIN="$(python3 -c 'import shutil; print(shutil.which(\"cfleet\") or \"\")')"
        if [ -n "${CFLEET_BIN}" ] && [ "${CFLEET_BIN}" != "/usr/local/bin/cfleet" ]; then
            ${SUDO} ln -sf "${CFLEET_BIN}" /usr/local/bin/cfleet
        fi
        CFLEET_GH_BIN="$(python3 -c 'import shutil; print(shutil.which(\"cfleet-gh-token\") or \"\")')"
        if [ -n "${CFLEET_GH_BIN}" ] && [ "${CFLEET_GH_BIN}" != "/usr/local/bin/cfleet-gh-token" ]; then
            ${SUDO} ln -sf "${CFLEET_GH_BIN}" /usr/local/bin/cfleet-gh-token
        fi

        echo "==> Writing ~/.cfleet/config.yml for ${CFLEET_SSH_USER}..."
        USER_HOME="/home/${CFLEET_SSH_USER}"
        ${SUDO} mkdir -p "${USER_HOME}/.cfleet"
        ${SUDO} tee "${USER_HOME}/.cfleet/config.yml" >/dev/null << CFG_EOF
        anthropic_api_key: "${CFLEET_API_KEY}"
        model: "${CFLEET_MODEL}"
        server:
          url: "${CFLEET_SERVER_URL}"
          token: "${CFLEET_SERVER_TOKEN}"
        CFG_EOF
        ${SUDO} chown -R "${CFLEET_SSH_USER}:${CFLEET_SSH_USER}" "${USER_HOME}/.cfleet"
        ${SUDO} chmod 600 "${USER_HOME}/.cfleet/config.yml"

        echo "==> Installing machine-agent systemd unit..."
        ${SUDO} tee /etc/systemd/system/cfleet-machine-agent.service >/dev/null << UNIT_EOF
        [Unit]
        Description=Claude Fleet machine agent
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=simple
        User=${CFLEET_SSH_USER}
        WorkingDirectory=${USER_HOME}
        Environment=HOME=${USER_HOME}
        Environment=PATH=${USER_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin
        Environment=ANTHROPIC_API_KEY=${CFLEET_API_KEY}
        ExecStart=/usr/local/bin/cfleet machine agent --name ${CFLEET_MACHINE_NAME} --server-url ${CFLEET_SERVER_URL} --token ${CFLEET_SERVER_TOKEN}
        Restart=on-failure
        RestartSec=5

        [Install]
        WantedBy=multi-user.target
        UNIT_EOF

        ${SUDO} systemctl daemon-reload
        ${SUDO} systemctl enable --now cfleet-machine-agent.service

        echo "==> Machine bootstrap complete."
        echo "==> Verify with: sudo systemctl status cfleet-machine-agent.service"
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
    machine_name: str,
) -> None:
    """Bootstrap a cloud VM into a fleet machine.

    Installs cfleet, writes ~/.cfleet/config.yml, and starts the machine-agent
    as a systemd service so the VM self-registers with the fleet server.
    After this returns, no further provisioning is needed — workers can be
    spawned on the machine via the dashboard / API like any external host.
    """
    _stage_files(ip, user, key_path, "staging", fleet_config)

    env_vars = {
        "CFLEET_SSH_USER": user,
        "CFLEET_API_KEY": fleet_config.anthropic_api_key,
        "CFLEET_MODEL": fleet_config.model,
        "CFLEET_SERVER_URL": fleet_config.server.url,
        "CFLEET_SERVER_TOKEN": fleet_config.server.token,
        "CFLEET_MACHINE_NAME": machine_name,
        "CFLEET_REPO": fleet_config.repo_url or "https://github.com/AmeanAsad/claude-fleet.git",
        "CFLEET_BRANCH": fleet_config.repo_branch or "fleat/v2-fleet",
    }

    ssh_run_script(ip, user, key_path, _bootstrap_machine_sh(), env_vars=env_vars, timeout=900)


# ---------------------------------------------------------------------------
# Local provisioning — used by `cfleet join` and `cfleet agent`
# ---------------------------------------------------------------------------


def _run(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True, **kwargs)


def local_bootstrap(api_key: str, model: str) -> None:
    """Bootstrap the local machine: install system deps, Claude Code, relay deps.

    Same logic as _bootstrap_machine_sh() but run locally via subprocess.
    """
    user = os.environ.get("USER", "ubuntu")
    home = Path.home()

    print("==> Installing system packages...")
    apt = shutil.which("apt-get")
    if apt:
        _run(["sudo", "apt-get", "update", "-y"])
        _run(["sudo", "apt-get", "install", "-y",
              "git", "curl", "rsync", "build-essential", "ripgrep", "jq",
              "wget", "unzip", "libssl-dev", "pkg-config", "python3-pip",
              "fd-find", "ncurses-bin"])

    print("==> Installing Claude Code CLI...")
    claude_bin = home / ".local" / "bin" / "claude"
    if not claude_bin.exists():
        subprocess.run("curl -fsSL https://claude.ai/install.sh | bash",
                        shell=True, check=True)

    print("==> Configuring Claude Code...")
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    claude_dir.chmod(0o700)

    api_key_file = claude_dir / ".api-key"
    api_key_file.write_text(api_key)
    api_key_file.chmod(0o600)

    settings = {
        "model": model,
        "alwaysThinkingEnabled": True,
        "skipDangerousModePermissionPrompt": True,
        "apiKeyHelper": f"cat {api_key_file}",
        "effortLevel": "high",
        "permissions": {
            "allow": ["Read", "Write", "Edit", "MultiEdit", "Bash(*)", "WebFetch"]
        },
    }
    settings_file = claude_dir / "settings.json"
    settings_file.write_text(json.dumps(settings, indent=2))
    settings_file.chmod(0o600)

    onboard_file = home / ".claude.json"
    onboard_file.write_text(json.dumps({
        "hasCompletedOnboarding": True,
        "hasAcknowledgedDisclaimer": True,
        "effortCalloutDismissed": True,
        "projects": {
            "/workspace": {"hasTrustDialogAccepted": True, "allowedTools": []},
            str(home): {"hasTrustDialogAccepted": True, "allowedTools": []},
        },
    }, indent=2))

    trust_file = claude_dir / "claude.json"
    trust_file.write_text(json.dumps({
        "hasCompletedOnboarding": True,
        "hasTrustDialogAccepted": True,
        "hasTrustDialogHooksAccepted": True,
        "hasCompletedProjectOnboarding": True,
    }, indent=2))

    bashrc = home / ".bashrc"
    bashrc_text = bashrc.read_text() if bashrc.exists() else ""
    if "CLAUDE_CODE_API_KEY" not in bashrc_text:
        with open(bashrc, "a") as f:
            f.write(f'\nexport CLAUDE_CODE_API_KEY="{api_key}"\n')
    if ".local/bin" not in bashrc_text:
        with open(bashrc, "a") as f:
            f.write(f'\nexport PATH="{home}/.local/bin:$PATH"\n')

    print("==> Installing relay Python dependencies...")
    try:
        _run(["pip3", "install", "--break-system-packages", "--quiet",
              "claude-code-sdk", "httpx", "fastapi", "uvicorn", "sse-starlette",
              "pydantic", "PyJWT", "cryptography", "websockets"])
    except subprocess.CalledProcessError:
        _run(["pip3", "install", "--quiet",
              "claude-code-sdk", "httpx", "fastapi", "uvicorn", "sse-starlette",
              "pydantic", "PyJWT", "cryptography", "websockets"])

    print("==> Deploying relay scripts...")
    relay_dir = Path("/opt/cfleet-relay")
    module_dir = Path(__file__).parent
    try:
        _run(["sudo", "mkdir", "-p", str(relay_dir)])
        for script_name in ("worker_relay.py", "credential_helper.py"):
            src = module_dir / script_name
            if src.exists():
                _run(["sudo", "cp", str(src), str(relay_dir / script_name)])
        _run(["sudo", "chown", "-R", f"{user}:{user}", str(relay_dir)])
    except subprocess.CalledProcessError:
        relay_dir = home / ".cfleet" / "relay"
        relay_dir.mkdir(parents=True, exist_ok=True)
        for script_name in ("worker_relay.py", "credential_helper.py"):
            src = module_dir / script_name
            if src.exists():
                shutil.copy(src, relay_dir / script_name)

    print("==> Local bootstrap complete.")


def local_provision_worker(
    worker_name: str,
    relay_port: int,
    model: str,
    repos: list[dict],
    fleet_config,
    workspace: str | None = None,
) -> str:
    """Provision a worker workspace locally. Returns the workspace path.

    Same logic as _provision_worker_sh() but run locally — no systemd, no SSH.
    The caller is responsible for starting the relay process.
    """
    home = Path.home()
    ws = Path(workspace) if workspace else home / "workspace"
    worker_dir = ws / worker_name
    worker_dir.mkdir(parents=True, exist_ok=True)
    for d in ("repos", "inbox", "outbox"):
        (worker_dir / d).mkdir(exist_ok=True)

    if not (worker_dir / ".git").exists():
        subprocess.run(["git", "init"], cwd=str(worker_dir),
                        capture_output=True, check=False)

    if repos:
        for r in repos:
            dest = worker_dir / "repos" / r["name"]
            if dest.exists():
                print(f"  Repo {r['name']} already cloned, skipping")
                continue
            branch = r.get("branch", "main")
            subprocess.run(
                ["git", "clone", "--depth", "1", "--single-branch",
                 "-b", branch, r["url"], str(dest)],
                check=True,
            )
            print(f"  Cloned {r['name']} ({branch})")

    claude_md = fleet_config.resolve_claude_md()
    if claude_md.exists():
        shutil.copy(claude_md, worker_dir / "CLAUDE.md")

    skills_dir = fleet_config.resolve_skills_dir()
    if skills_dir.exists():
        dest_skills = home / ".claude" / "skills"
        if dest_skills.exists():
            shutil.rmtree(dest_skills)
        shutil.copytree(skills_dir, dest_skills)

    mcp_config = fleet_config.resolve_mcp_config()
    if mcp_config.exists():
        shutil.copy(mcp_config, home / ".claude" / "mcp-servers.json")

    secrets_env = fleet_config.resolve_secrets_env()
    if secrets_env.exists():
        dest_env = home / ".cfleet-env"
        shutil.copy(secrets_env, dest_env)
        dest_env.chmod(0o600)

    return str(worker_dir)
