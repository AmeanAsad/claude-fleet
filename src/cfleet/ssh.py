"""SSH operations — subprocess-based, no paramiko."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
    "-o", "LogLevel=ERROR",
    "-o", "ControlMaster=auto",
    "-o", "ControlPersist=300",
]


def _control_path(ip: str) -> list[str]:
    return ["-o", f"ControlPath=/tmp/cfleet-ssh-{ip}-%r"]


def _ssh_base(ip: str, user: str, key_path: str) -> list[str]:
    key = str(Path(key_path).expanduser())
    return ["ssh", "-i", key, *SSH_OPTS, *_control_path(ip), f"{user}@{ip}"]


def ssh_run(
    ip: str,
    user: str,
    key_path: str,
    command: str,
    timeout: int = 30,
) -> tuple[str, str, int]:
    """Run a command on a remote machine. Returns (stdout, stderr, exit_code)."""
    result = subprocess.run(
        [*_ssh_base(ip, user, key_path), command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout, result.stderr, result.returncode


def ssh_run_script(
    ip: str,
    user: str,
    key_path: str,
    script: str,
    env_vars: dict[str, str] | None = None,
    timeout: int = 600,
) -> None:
    """Pipe a script to bash on a remote machine."""
    env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in (env_vars or {}).items())
    remote_cmd = f"{env_prefix} bash -s" if env_prefix else "bash -s"
    subprocess.run(
        [*_ssh_base(ip, user, key_path), remote_cmd],
        input=script,
        text=True,
        check=True,
        timeout=timeout,
    )


def wait_for_ssh(
    ip: str,
    user: str,
    key_path: str,
    timeout: int = 300,
    interval: int = 5,
) -> None:
    """Poll until SSH is available. Raises TimeoutError."""
    key = str(Path(key_path).expanduser())
    start = time.time()
    while time.time() - start < timeout:
        result = subprocess.run(
            ["ssh", "-i", key, *SSH_OPTS, *_control_path(ip),
             f"{user}@{ip}", "true"],
            capture_output=True,
            timeout=15,
        )
        if result.returncode == 0:
            return
        time.sleep(interval)
    raise TimeoutError(f"SSH not available on {ip} after {timeout}s")


def rsync_to(
    ip: str,
    user: str,
    key_path: str,
    local_path: str,
    remote_path: str,
) -> None:
    """rsync local files to remote."""
    key = str(Path(key_path).expanduser())
    ssh_cmd = f"ssh -i {key} {' '.join(SSH_OPTS)} {' '.join(_control_path(ip))}"
    subprocess.run(
        ["rsync", "-avz", "--filter=:- .gitignore",
         "-e", ssh_cmd,
         local_path.rstrip("/"),
         f"{user}@{ip}:{remote_path}/"],
        check=True,
    )


def rsync_from(
    ip: str,
    user: str,
    key_path: str,
    remote_path: str,
    local_path: str,
) -> None:
    """rsync remote files to local."""
    key = str(Path(key_path).expanduser())
    Path(local_path).mkdir(parents=True, exist_ok=True)
    ssh_cmd = f"ssh -i {key} {' '.join(SSH_OPTS)} {' '.join(_control_path(ip))}"
    subprocess.run(
        ["rsync", "-avz", "--filter=:- .gitignore",
         "-e", ssh_cmd,
         f"{user}@{ip}:{remote_path}/",
         f"{local_path}/"],
        check=True,
    )


def ssh_attach(
    ip: str,
    user: str,
    key_path: str,
    command: str = "bash -l",
) -> None:
    """Replace current process with an interactive SSH session."""
    key = str(Path(key_path).expanduser())
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    os.execve(
        "/usr/bin/ssh",
        ["ssh", "-t", "-i", key,
         "-o", "StrictHostKeyChecking=no",
         f"{user}@{ip}", command],
        env,
    )
