"""Devcontainer provider — runs fleet workers in local Docker containers.

Based on Trail of Bits' claude-code-devcontainer approach: an Ubuntu 24.04
container with Claude Code pre-installed, tmux, and /workspace mounted.

No Pulumi, no SSH, no Ansible. Everything goes through `docker exec`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

from rich.console import Console

from cfleet.relay_client import RelayClient

console = Console()

# Container image name for fleet workers
FLEET_IMAGE = "cfleet-worker"
FLEET_DOCKERFILE = Path(__file__).parent / "devcontainer" / "Dockerfile"

# Labels applied to every fleet container for identification
FLEET_LABEL = "cfleet.worker"

# Default relay port inside containers
RELAY_PORT = 8421


def _run(cmd: list[str], check: bool = True, capture: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, **kwargs)


def docker_available() -> bool:
    """Check if Docker is installed and the daemon is reachable."""
    try:
        _run(["docker", "info"])
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def image_exists() -> bool:
    """Check if the fleet worker image has been built."""
    r = _run(["docker", "images", "-q", FLEET_IMAGE], check=False)
    return bool(r.stdout.strip())


def build_image(force: bool = False) -> None:
    """Build the fleet worker Docker image."""
    if image_exists() and not force:
        return
    if not FLEET_DOCKERFILE.exists():
        raise FileNotFoundError(f"Dockerfile not found at {FLEET_DOCKERFILE}")
    console.print(f"Building [bold]{FLEET_IMAGE}[/bold] image...")
    subprocess.run(
        ["docker", "build", "-t", FLEET_IMAGE, str(FLEET_DOCKERFILE.parent)],
        check=True,
    )


def _get_container_ip(container_id: str) -> str:
    """Get the container's IP address on the Docker bridge network."""
    r = _run([
        "docker", "inspect", "-f",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
        container_id,
    ], check=False)
    return r.stdout.strip()


def spawn_container(
    name: str,
    anthropic_api_key: str,
    model: str,
    repos: list[dict],
    fleet_config,
) -> str:
    """Create and start a fleet worker container. Returns the container ID."""
    build_image()

    # Env vars injected into the container
    env = {
        "ANTHROPIC_API_KEY": anthropic_api_key,
        "CLAUDE_CODE_API_KEY": anthropic_api_key,
        "CFLEET_MODEL": model,
    }

    # Read additional secrets from secrets.env
    secrets_env = fleet_config.resolve_secrets_env()
    if secrets_env.exists():
        for line in secrets_env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()

    cmd = [
        "docker", "run", "-d",
        "--name", f"cfleet-{name}",
        "--hostname", name,
        "--label", f"{FLEET_LABEL}={name}",
        "--init",
        "--cap-add=NET_ADMIN",
        "--cap-add=NET_RAW",
    ]

    for k, v in env.items():
        cmd.extend(["-e", f"{k}={v}"])

    cmd.append(FLEET_IMAGE)
    # Keep container alive
    cmd.extend(["sleep", "infinity"])

    r = _run(cmd)
    container_id = r.stdout.strip()

    # Provision inside the container
    _provision_container(name, container_id, model, repos, fleet_config)

    return container_id


def _provision_container(
    name: str,
    container_id: str,
    model: str,
    repos: list[dict],
    fleet_config,
) -> None:
    """Set up workspace, repos, claude config, and relay inside the container."""
    user = "vscode"

    def _exec(cmd: str, user: str = user) -> str:
        r = _run(["docker", "exec", "-u", user, container_id, "bash", "-c", cmd])
        return r.stdout

    # Create workspace dirs
    _exec("mkdir -p /workspace/repos /workspace/inbox /workspace/outbox", user="root")
    _exec("chown -R vscode:vscode /workspace", user="root")

    # Git init workspace so Claude skips trust prompt
    _exec("cd /workspace && git init")

    # Clone repos
    for repo in repos:
        repo_name = repo["name"]
        url = repo["url"]
        branch = repo.get("branch", "main")
        _exec(f"git clone --depth 1 --single-branch -b {branch} {url} /workspace/repos/{repo_name}")
        _exec(
            f"cd /workspace/repos/{repo_name} && "
            f"git remote set-url --push origin no_push_allowed && "
            f"mkdir -p .git/hooks && "
            f"printf '#!/bin/sh\\necho Push disabled on fleet workers\\nexit 1\\n' > .git/hooks/pre-push && "
            f"chmod +x .git/hooks/pre-push"
        )

    # Deploy CLAUDE.md
    claude_md = fleet_config.resolve_claude_md()
    if claude_md.exists():
        _docker_cp(container_id, str(claude_md), "/workspace/CLAUDE.md", owner=user)

    # Deploy skills
    skills_dir = fleet_config.resolve_skills_dir()
    if skills_dir.exists() and any(skills_dir.iterdir()):
        _exec("mkdir -p /home/vscode/.claude/skills")
        _docker_cp(container_id, str(skills_dir), "/home/vscode/.claude/skills/", owner=user)

    # Deploy MCP config
    mcp_config = fleet_config.resolve_mcp_config()
    if mcp_config.exists():
        _docker_cp(container_id, str(mcp_config), "/home/vscode/.claude/mcp-servers.json", owner=user)

    # Claude Code settings
    # Store API key in a file and use cat to read it (avoids shell injection via echo)
    api_key = fleet_config.anthropic_api_key
    _exec("mkdir -p /home/vscode/.claude")
    _exec(f"cat > /home/vscode/.claude/.api-key << 'CFLEET_EOF'\n{api_key}\nCFLEET_EOF")
    _exec("chmod 600 /home/vscode/.claude/.api-key")
    settings = {
        "model": model,
        "alwaysThinkingEnabled": True,
        "skipDangerousModePermissionPrompt": True,
        "apiKeyHelper": "cat /home/vscode/.claude/.api-key",
        "effortLevel": "high",
        "permissions": {
            "allow": ["Read", "Write", "Edit", "MultiEdit", "Bash(*)", "WebFetch"],
        },
    }
    settings_json = json.dumps(settings)
    _exec(f"mkdir -p /home/vscode/.claude")
    _exec(f"cat > /home/vscode/.claude/settings.json << 'CFLEET_EOF'\n{settings_json}\nCFLEET_EOF")

