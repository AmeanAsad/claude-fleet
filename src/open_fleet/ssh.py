"""SSH operations for fleet workers — exec, tmux, rsync."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import paramiko


class WorkerSSH:
    """SSH connection to a single fleet worker."""

    def __init__(self, ip: str, user: str, key_path: str):
        self.ip = ip
        self.user = user
        self.key_path = str(Path(key_path).expanduser())
        self._client: paramiko.SSHClient | None = None

    def _connect(self) -> paramiko.SSHClient:
        if self._client is not None:
            # Check if the connection is still alive
            try:
                self._client.exec_command("true", timeout=5)
                return self._client
            except Exception:
                self._client = None

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.ip,
            username=self.user,
            key_filename=self.key_path,
            timeout=15,
        )
        self._client = client
        return client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def exec(self, cmd: str, timeout: int = 30) -> tuple[str, str, int]:
        """Run a command, return (stdout, stderr, exit_code)."""
        client = self._connect()
        _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        return stdout.read().decode(), stderr.read().decode(), exit_code

    def send_prompt(self, text: str) -> None:
        """Inject text into the tmux claude session.

        Sends Escape first to dismiss any TUI notifications/popups,
        then uses -l (literal) flag to send text without interpreting
        special chars, then sends Enter separately.
        """
        # Dismiss any popup/notification that might be capturing input
        self.exec("tmux send-keys -t claude:code Escape")
        time.sleep(0.5)
        escaped = text.replace("'", "'\\''")
        self.exec(f"tmux send-keys -t claude:code -l '{escaped}'")
        self.exec("tmux send-keys -t claude:code Enter")

    def read_logs(self, lines: int = 100) -> str:
        """Capture recent tmux scrollback."""
        stdout, _, _ = self.exec(f"tmux capture-pane -t claude:code -p -S -{lines}")
        return stdout

    def stream_logs(self, interval: float = 2.0) -> Iterator[str]:
        """Tail tmux output by polling. Yields new lines.

        Polls tmux capture-pane on interval. This is not efficient but is simple.
        Could be improved with inotifywait on a log file.
        """
        seen_lines: set[str] = set()
        while True:
            try:
                output = self.read_logs(lines=200)
                for line in output.splitlines():
                    if line and line not in seen_lines:
                        seen_lines.add(line)
                        yield line
                # Cap the seen set to prevent unbounded growth
                if len(seen_lines) > 5000:
                    seen_lines = set(list(seen_lines)[-2000:])
            except Exception:
                yield "[connection lost, retrying...]"
            time.sleep(interval)

    def is_claude_idle(self) -> bool:
        """Check if Claude Code is waiting for input.

        Captures the last few lines of the visible pane and looks for the
        ❯ prompt character that Claude Code displays when idle.
        The bottom line is typically a status bar (e.g. "⏵⏵ bypass permissions on"),
        so we check all recent lines for the prompt.
        """
        stdout, _, _ = self.exec("tmux capture-pane -t claude:code -p -S -5")
        lines = [l.strip() for l in stdout.splitlines() if l.strip()]
        # Look for the ❯ prompt anywhere in the last few lines
        return any(l == "❯" or l.startswith("❯ ") for l in lines)

    def is_alive(self) -> bool:
        """Check if tmux claude session exists."""
        _, _, exit_code = self.exec("tmux has-session -t claude 2>/dev/null")
        return exit_code == 0

    def attach(self) -> None:
        """Attach to the worker's tmux session. Detaching lands on a bash shell."""
        os.execvp(
            "ssh",
            [
                "ssh",
                "-t",
                "-i", self.key_path,
                "-o", "StrictHostKeyChecking=no",
                f"{self.user}@{self.ip}",
                "tmux", "attach", "-t", "claude",
            ],
        )

    def send_files(self, local_path: str, remote_path: str = "/workspace/inbox/") -> None:
        """rsync local files to worker, preserving directory name."""
        # No trailing slash on local_path so rsync copies the directory itself
        # (e.g. ./ansible → inbox/ansible/) rather than just its contents.
        local = local_path.rstrip("/")
        subprocess.run(
            [
                "rsync", "-avz",
                "--filter=:- .gitignore",
                "-e", f"ssh -i {self.key_path} -o StrictHostKeyChecking=no",
                local,
                f"{self.user}@{self.ip}:{remote_path}/",
            ],
            check=True,
        )

    def collect(self, remote_path: str, local_path: str) -> None:
        """rsync worker files to local, respecting .gitignore."""
        Path(local_path).mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "rsync", "-avz",
                "--filter=:- .gitignore",
                "-e", f"ssh -i {self.key_path} -o StrictHostKeyChecking=no",
                f"{self.user}@{self.ip}:{remote_path}/",
                f"{local_path}/",
            ],
            check=True,
        )


def wait_for_ssh(ip: str, user: str, key_path: str, timeout: int = 300, interval: int = 5) -> None:
    """Poll until SSH is available. Raises TimeoutError."""
    key = str(Path(key_path).expanduser())
    start = time.time()
    while time.time() - start < timeout:
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(ip, username=user, key_filename=key, timeout=10)
            client.close()
            return
        except (
            paramiko.ssh_exception.NoValidConnectionsError,
            paramiko.ssh_exception.SSHException,
            socket.timeout,
            OSError,
        ):
            time.sleep(interval)
    raise TimeoutError(f"SSH not available on {ip} after {timeout}s")
