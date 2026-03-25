"""SSH operations for fleet workers — exec, relay, rsync."""

from __future__ import annotations

import os
import select
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import logging

import paramiko

from cfleet.relay_client import RelayClient

# Suppress paramiko's noisy transport-thread tracebacks during SSH polling
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

# Default port the worker relay listens on
RELAY_PORT = 8421


class WorkerSSH:
    """SSH connection to a single fleet worker.

    Provides exec, file transfer, and access to the Agent SDK relay.
    Legacy tmux methods are kept for migration compatibility.
    """

    def __init__(self, ip: str, user: str, key_path: str, relay_port: int = RELAY_PORT):
        self.ip = ip
        self.user = user
        self.key_path = str(Path(key_path).expanduser())
        self.relay_port = relay_port
        self._client: paramiko.SSHClient | None = None
        self._tunnel_local_port: int | None = None
        self._tunnel_thread: threading.Thread | None = None
        self._tunnel_stop: bool = False

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
        self._tunnel_stop = True
        if self._client is not None:
            self._client.close()
            self._client = None
        self._tunnel_local_port = None

    def exec(self, cmd: str, timeout: int = 30) -> tuple[str, str, int]:
        """Run a command, return (stdout, stderr, exit_code)."""
        client = self._connect()
        _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        return stdout.read().decode(), stderr.read().decode(), exit_code

    # ------------------------------------------------------------------
    # Relay management
    # ------------------------------------------------------------------

    def start_relay(self, model: str = "", cwd: str = "/workspace") -> None:
        """Start the worker relay on the remote machine if not already running.

        On cloud VMs the relay is deployed to /opt/cfleet-relay/worker_relay.py
        by Ansible, or managed by a systemd unit. This method is a fallback for
        when the systemd service isn't running.
        """
        _, _, exit_code = self.exec(
            f"curl -sf http://127.0.0.1:{self.relay_port}/health", timeout=5
        )
        if exit_code == 0:
            return  # Already running

        # Try restarting via systemd first (preferred on cloud VMs)
        _, _, rc = self.exec("sudo systemctl restart cfleet-relay 2>/dev/null", timeout=10)
        if rc == 0:
            for _ in range(15):
                time.sleep(1)
                _, _, exit_code = self.exec(
                    f"curl -sf http://127.0.0.1:{self.relay_port}/health", timeout=5
                )
                if exit_code == 0:
                    return

        # Fallback: start directly
        model_arg = f"--model {model}" if model else ""
        self.exec(
            f"nohup python3 /opt/cfleet-relay/worker_relay.py "
            f"--port {self.relay_port} --host 127.0.0.1 "
            f"{model_arg} --cwd {cwd} "
            f"> /tmp/cfleet-relay.log 2>&1 &",
            timeout=10,
        )
        for _ in range(15):
            time.sleep(1)
            _, _, exit_code = self.exec(
                f"curl -sf http://127.0.0.1:{self.relay_port}/health", timeout=5
            )
            if exit_code == 0:
                return
        raise RuntimeError("Worker relay failed to start within 15 seconds")

    def open_tunnel(self) -> int:
        """Open an SSH tunnel to the relay port. Returns the local port."""
        if self._tunnel_local_port is not None:
            return self._tunnel_local_port

        client = self._connect()
        transport = client.get_transport()
        if transport is None:
            raise RuntimeError("SSH transport not available")

        # Find a free local port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            local_port = s.getsockname()[1]

        self._tunnel_local_port = local_port
        self._tunnel_stop = False

        def tunnel_listener():
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", local_port))
            server.listen(5)
            server.settimeout(1.0)
            while not self._tunnel_stop:
                try:
                    client_sock, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    chan = transport.open_channel(
                        "direct-tcpip",
                        ("127.0.0.1", self.relay_port),
                        client_sock.getpeername(),
                    )
                except Exception:
                    client_sock.close()
                    continue
                if chan is None:
                    client_sock.close()
                    continue
                t = threading.Thread(
                    target=_forward, args=(client_sock, chan), daemon=True
                )
                t.start()
            server.close()

        self._tunnel_thread = threading.Thread(target=tunnel_listener, daemon=True)
        self._tunnel_thread.start()
        return local_port

    def get_relay_client(self) -> RelayClient:
        """Return a RelayClient connected through an SSH tunnel."""
        local_port = self.open_tunnel()
        return RelayClient(f"http://127.0.0.1:{local_port}")

    # ------------------------------------------------------------------
    # Legacy tmux methods (kept for migration from tmux-based workers)
    # ------------------------------------------------------------------

    def send_prompt(self, text: str) -> None:
        """Inject text into the tmux claude session (legacy)."""
        self.exec("tmux send-keys -t claude:code Escape")
        time.sleep(0.5)
        escaped = text.replace("'", "'\\''")
        self.exec(f"tmux send-keys -t claude:code -l '{escaped}'")
        self.exec("tmux send-keys -t claude:code Enter")

    def read_logs(self, lines: int = 100) -> str:
        """Capture recent tmux scrollback (legacy)."""
        stdout, _, _ = self.exec(f"tmux capture-pane -t claude:code -p -S -{lines}")
        return stdout

    def stream_logs(self, interval: float = 2.0) -> Iterator[str]:
        """Tail tmux output by polling (legacy). Yields new lines."""
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
        """Check if Claude Code is waiting for input (legacy)."""
        stdout, _, _ = self.exec("tmux capture-pane -t claude:code -p -S -5")
        lines = [l.strip() for l in stdout.splitlines() if l.strip()]
        return any(l == "\u276f" or l.startswith("\u276f ") for l in lines)

    def is_alive(self) -> bool:
        """Check if tmux claude session exists (legacy)."""
        _, _, exit_code = self.exec("tmux has-session -t claude 2>/dev/null")
        return exit_code == 0

    def attach(self) -> None:
        """Attach to the worker via SSH bash shell."""
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        os.execve(
            "/usr/bin/ssh",
            [
                "ssh",
                "-t",
                "-i", self.key_path,
                "-o", "StrictHostKeyChecking=no",
                f"{self.user}@{self.ip}",
                "bash", "-l",
            ],
            env,
        )

    def send_files(self, local_path: str, remote_path: str = "/workspace/inbox/") -> None:
        """rsync local files to worker, preserving directory name."""
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


def _forward(sock: socket.socket, chan: paramiko.Channel) -> None:
    """Bidirectional forwarding between a socket and SSH channel."""
    while True:
        r, _, _ = select.select([sock, chan], [], [], 1.0)
        if sock in r:
            data = sock.recv(4096)
            if not data:
                break
            chan.sendall(data)
        if chan in r:
            data = chan.recv(4096)
            if not data:
                break
            sock.sendall(data)
    sock.close()
    chan.close()


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
