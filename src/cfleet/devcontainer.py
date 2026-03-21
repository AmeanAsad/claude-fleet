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

console = Console()

# Container image name for fleet workers
FLEET_IMAGE = "cfleet-worker"
FLEET_DOCKERFILE = Path(__file__).parent / "devcontainer" / "Dockerfile"

# Labels applied to every fleet container for identification
FLEET_LABEL = "cfleet.worker"


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
    """Set up workspace, repos, claude config, tmux inside the container."""
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
    api_key = fleet_config.anthropic_api_key
    settings = {
        "model": model,
        "alwaysThinkingEnabled": True,
        "skipDangerousModePermissionPrompt": True,
        "apiKeyHelper": f"echo {api_key}",
        "effortLevel": "high",
        "permissions": {
            "allow": ["Read", "Write", "Edit", "MultiEdit", "Bash(*)", "WebFetch"],
        },
    }
    settings_json = json.dumps(settings)
    _exec(f"mkdir -p /home/vscode/.claude")
    _exec(f"cat > /home/vscode/.claude/settings.json << 'CFLEET_EOF'\n{settings_json}\nCFLEET_EOF")

    # Pre-accept onboarding
    onboarding = json.dumps({
        "hasCompletedOnboarding": True,
        "hasAcknowledgedDisclaimer": True,
        "effortCalloutDismissed": True,
        "projects": {
            "/workspace": {"hasTrustDialogAccepted": True, "allowedTools": []},
            "/home/vscode": {"hasTrustDialogAccepted": True, "allowedTools": []},
        },
    })
    _exec(f"cat > /home/vscode/.claude.json << 'CFLEET_EOF'\n{onboarding}\nCFLEET_EOF")

    trust = json.dumps({
        "hasCompletedOnboarding": True,
        "hasTrustDialogAccepted": True,
        "hasTrustDialogHooksAccepted": True,
        "hasCompletedProjectOnboarding": True,
    })
    _exec(f"cat > /home/vscode/.claude/claude.json << 'CFLEET_EOF'\n{trust}\nCFLEET_EOF")

    # Start tmux session
    _exec(
        "export TERM=xterm-256color && "
        "tmux new-session -d -s claude -n code -c /workspace && "
        f"tmux send-keys -t claude:code 'claude --model {model} --dangerously-skip-permissions' Enter && "
        "sleep 5 && "
        "tmux send-keys -t claude:code Enter && "
        "sleep 2 && "
        "tmux send-keys -t claude:code Enter && "
        "tmux new-window -t claude -n bash -c /workspace && "
        "tmux select-window -t claude:code"
    )


def _docker_cp(container_id: str, src: str, dest: str, owner: str | None = None) -> None:
    """Copy a file or directory from host into the container."""
    _run(["docker", "cp", src, f"{container_id}:{dest}"])
    if owner:
        _run(["docker", "exec", "-u", "root", container_id, "chown", "-R", f"{owner}:{owner}", dest])


def kill_container(name: str) -> None:
    """Stop and remove a fleet container."""
    cname = f"cfleet-{name}"
    _run(["docker", "rm", "-f", cname], check=False)


def get_container_id(name: str) -> str:
    """Get the container ID for a named worker."""
    r = _run(
        ["docker", "ps", "-q", "--filter", f"label={FLEET_LABEL}={name}"],
        check=False,
    )
    return r.stdout.strip()


# ---------------------------------------------------------------------------
# WorkerDocker — same interface as WorkerSSH for engine interop
# ---------------------------------------------------------------------------


class WorkerDocker:
    """Docker exec connection to a fleet worker container.

    Mirrors the WorkerSSH interface so the engine can use either transparently.
    """

    def __init__(self, container_id: str, user: str = "vscode"):
        self.container_id = container_id
        self.user = user

    def close(self) -> None:
        pass  # No persistent connection to close

    def exec(self, cmd: str, timeout: int = 30) -> tuple[str, str, int]:
        """Run a command, return (stdout, stderr, exit_code)."""
        r = subprocess.run(
            ["docker", "exec", "-u", self.user, self.container_id, "bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.stdout, r.stderr, r.returncode

    def send_prompt(self, text: str) -> None:
        """Inject text into the tmux claude session."""
        self.exec("tmux send-keys -t claude:code Escape")
        time.sleep(0.5)
        escaped = text.replace("'", "'\\''")
        self.exec(f"tmux send-keys -t claude:code -l '{escaped}'")
        self.exec("tmux send-keys -t claude:code Enter")

    def read_logs(self, lines: int = 100) -> str:
        stdout, _, _ = self.exec(f"tmux capture-pane -t claude:code -p -S -{lines}")
        return stdout

    def stream_logs(self, interval: float = 2.0) -> Iterator[str]:
        prev_output = ""
        while True:
            try:
                output = self.read_logs(lines=200)
                if output != prev_output:
                    prev_lines = prev_output.splitlines()
                    curr_lines = output.splitlines()
                    overlap = 0
                    if prev_lines:
                        for i in range(len(curr_lines)):
                            if curr_lines[i:i + len(prev_lines)] == prev_lines:
                                overlap = i + len(prev_lines)
                                break
                    new_lines = curr_lines[overlap:]
                    for line in new_lines:
                        yield line
                    prev_output = output
            except Exception:
                yield "[connection lost, retrying...]"
            time.sleep(interval)

    def is_claude_idle(self) -> bool:
        stdout, _, _ = self.exec("tmux capture-pane -t claude:code -p -S -5")
        lines = [l.strip() for l in stdout.splitlines() if l.strip()]
        return any(l == "\u276f" or l.startswith("\u276f ") for l in lines)

    def is_alive(self) -> bool:
        _, _, exit_code = self.exec("tmux has-session -t claude 2>/dev/null")
        return exit_code == 0

    def attach(self) -> None:
        """Attach to the container's tmux session. Replaces current process."""
        os.execvp(
            "docker",
            ["docker", "exec", "-it", "-u", self.user, self.container_id,
             "tmux", "attach", "-t", "claude"],
        )

    def send_files(self, local_path: str, remote_path: str = "/workspace/inbox/") -> None:
        local = local_path.rstrip("/")
        _docker_cp(self.container_id, local, remote_path, owner=self.user)

    def collect(self, remote_path: str, local_path: str) -> None:
        Path(local_path).mkdir(parents=True, exist_ok=True)
        remote = remote_path.rstrip("/")
        _run(["docker", "cp", f"{self.container_id}:{remote}/.", local_path])
