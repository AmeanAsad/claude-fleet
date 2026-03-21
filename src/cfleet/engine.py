"""Core orchestration engine — ties infra, provisioner, and SSH together."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from cfleet.config import CLOUD_PROVIDERS, DEFAULT_SKUS, PROVIDER_DEFAULTS, FleetConfig, FleetState, VMType, WorkerState

console = Console()


class FleetEngine:
    """Main orchestration entry point. All CLI/TUI commands go through here."""

    def __init__(self, config: FleetConfig | None = None, state: FleetState | None = None):
        self.config = config or FleetConfig.load()
        self.state = state or FleetState.load()
        self._infra = None  # Lazy — only needed for cloud providers

    @property
    def infra(self):
        if self._infra is None:
            from cfleet.infra import InfraManager
            self._infra = InfraManager(self.config)
        return self._infra

    def _save_state(self) -> None:
        self.state.save()

    def _get_conn(self, worker: WorkerState):
        """Return a WorkerSSH or WorkerDocker connection based on provider."""
        if worker.provider == "devcontainer":
            from cfleet.devcontainer import WorkerDocker
            return WorkerDocker(container_id=worker.container_id)
        from cfleet.ssh import WorkerSSH
        user = worker.ssh_user or self.config.resolve_ssh_user(provider=worker.provider)
        return WorkerSSH(
            ip=worker.ip,
            user=user,
            key_path=str(self.config.resolve_ssh_key()),
        )

    # ------------------------------------------------------------------
    # init
    # ------------------------------------------------------------------

    def init(self) -> None:
        """Initialize ~/.cfleet/ directory and Pulumi stack (if cloud provider)."""
        from cfleet.config import FLEET_DIR, CONFIG_PATH

        FLEET_DIR.mkdir(parents=True, exist_ok=True)
        (FLEET_DIR / "skills").mkdir(exist_ok=True)

        # Save config (includes any values collected during init prompts)
        if not CONFIG_PATH.exists():
            self.config.save()
            console.print(f"[green]Created config at {CONFIG_PATH}[/green]")
        else:
            self.config.save()
            console.print(f"[green]Updated config at {CONFIG_PATH}[/green]")

        claude_md_path = FLEET_DIR / "CLAUDE.md"
        if not claude_md_path.exists():
            defaults_dir = Path(__file__).parent / "defaults"
            if (defaults_dir / "CLAUDE.md").exists():
                import shutil
                shutil.copy(defaults_dir / "CLAUDE.md", claude_md_path)
            else:
                claude_md_path.write_text("# Fleet Worker Instructions\n\nYou are a fleet worker.\n")

        secrets_env_path = FLEET_DIR / "secrets.env"
        if not secrets_env_path.exists():
            secrets_env_path.write_text(
                f"# Fleet worker secrets — sourced as env vars on workers\n"
                f"ANTHROPIC_API_KEY={self.config.anthropic_api_key}\n"
            )
        elif self.config.anthropic_api_key:
            content = secrets_env_path.read_text()
            if "ANTHROPIC_API_KEY=sk-ant-..." in content or "ANTHROPIC_API_KEY=\n" in content:
                content = content.replace(
                    "ANTHROPIC_API_KEY=sk-ant-...",
                    f"ANTHROPIC_API_KEY={self.config.anthropic_api_key}",
                ).replace(
                    "ANTHROPIC_API_KEY=\n",
                    f"ANTHROPIC_API_KEY={self.config.anthropic_api_key}\n",
                )
                secrets_env_path.write_text(content)

        # Only init Pulumi if a cloud provider is configured
        if self.config.cloud.provider in CLOUD_PROVIDERS:
            (FLEET_DIR / "pulumi-state").mkdir(exist_ok=True)
            console.print("Initializing Pulumi stack...")
            self.infra.init_stack()

        if self.config.cloud.provider == "devcontainer":
            from cfleet.devcontainer import docker_available, build_image
            if not docker_available():
                console.print("[red]Docker is not available. Install Docker to use the devcontainer provider.[/red]")
                return
            console.print("Building devcontainer image...")
            build_image()

        console.print("[green]Fleet initialized.[/green] Edit ~/.cfleet/config.yml to configure.")

    # ------------------------------------------------------------------
    # spawn
    # ------------------------------------------------------------------

    def _validate_config(self, provider: str | None = None) -> None:
        """Ensure required config values are set before operating on infrastructure."""
        missing = []
        if not self.config.anthropic_api_key:
            missing.append("anthropic_api_key")
        effective_provider = provider or self.config.cloud.provider
        if not effective_provider:
            missing.append("cloud.provider (set in config or pass --provider)")
        elif effective_provider == "azure":
            if not self.config.cloud.azure.subscription_id:
                missing.append("cloud.azure.subscription_id")
            if not self.config.cloud.azure.resource_group:
                missing.append("cloud.azure.resource_group")
        elif effective_provider == "gcp":
            if not self.config.cloud.gcp.project_id:
                missing.append("cloud.gcp.project_id")
        elif effective_provider == "devcontainer":
            pass  # No cloud config needed
        if effective_provider in CLOUD_PROVIDERS:
            if not self.config.resolve_ssh_user(provider=effective_provider):
                missing.append("cloud.ssh_user")
        if missing:
            raise ValueError(
                f"Missing required config: {', '.join(missing)}. "
                "Run 'cfleet init' to set them."
            )

    def spawn(
        self,
        name: str,
        repos: list[str] | None = None,
        model: str | None = None,
        vm_type: VMType | None = None,
        instance_type: str | None = None,
        region: str | None = None,
        provider: str | None = None,
    ) -> WorkerState:
        """Spawn a new worker (cloud VM or local container)."""
        provider_name = provider or self.config.cloud.provider
        self._validate_config(provider=provider_name)
        if name in self.state.workers:
            raise ValueError(f"Worker '{name}' already exists. Kill it first or choose a different name.")

        effective_model = model or self.config.model
        effective_repos = repos or [r.name for r in self.config.repos]

        if provider_name == "devcontainer":
            return self._spawn_devcontainer(name, effective_model, effective_repos)

        return self._spawn_cloud(
            name, provider_name, effective_model, effective_repos,
            vm_type, instance_type, region,
        )

    def _spawn_devcontainer(self, name: str, model: str, repo_names: list[str]) -> WorkerState:
        """Spawn a worker as a local Docker container."""
        from cfleet.devcontainer import docker_available, spawn_container

        if not docker_available():
            raise RuntimeError("Docker is not available. Install Docker to use the devcontainer provider.")

        worker = WorkerState(
            name=name,
            status="spawning",
            provider="devcontainer",
            vm_type="container",
            instance_type="docker",
            ssh_user="vscode",
            model=model,
            repos=repo_names,
        )
        self.state.add_worker(worker)
        self._save_state()

        console.print(f"[bold]devcontainer[/bold] | Spawning container [bold]{name}[/bold]...")

        repo_configs = [r.model_dump() for r in self.config.repos if r.name in repo_names]

        container_id = spawn_container(
            name=name,
            anthropic_api_key=self.config.anthropic_api_key,
            model=model,
            repos=repo_configs,
            fleet_config=self.config,
        )

        worker.container_id = container_id
        worker.status = "idle"
        self._save_state()
        console.print(f"[green]Worker {name} ready (container {container_id[:12]})[/green]")
        return worker

    def _spawn_cloud(
        self,
        name: str,
        provider_name: str,
        model: str,
        repo_names: list[str],
        vm_type: VMType | None,
        instance_type: str | None,
        region: str | None,
    ) -> WorkerState:
        """Spawn a worker as a cloud VM via Pulumi + Ansible."""
        from cfleet.provisioner import bootstrap_worker
        from cfleet.ssh import wait_for_ssh

        effective_vm_type = vm_type or self.config.cloud.vm_type
        effective_instance_type = instance_type or self.config.resolve_instance_type(
            provider=provider_name, vm_type=effective_vm_type
        )

        console.print(
            f"[bold]{provider_name}[/bold] | "
            f"VM type: [bold]{effective_vm_type.value}[/bold] | "
            f"SKU: [bold]{effective_instance_type}[/bold]"
        )

        effective_ssh_user = self.config.resolve_ssh_user(provider=provider_name)
        worker = WorkerState(
            name=name,
            status="spawning",
            provider=provider_name,
            vm_type=effective_vm_type.value,
            instance_type=effective_instance_type,
            ssh_user=effective_ssh_user,
            model=model,
            repos=repo_names,
        )
        self.state.add_worker(worker)
        self._save_state()

        # 1. Create VM via Pulumi
        console.print(f"Creating VM for [bold]{name}[/bold]...")
        worker_cfg = {
            "instance_type": effective_instance_type,
            "vm_type": effective_vm_type.value,
            "provider": provider_name,
        }
        if region:
            if provider_name == "gcp":
                worker_cfg["zone"] = region
            else:
                worker_cfg["region"] = region

        outputs = self.infra.add_worker(name, worker_cfg, provider=provider_name)
        ip = outputs.get(f"{name}_ip", "")
        if not ip:
            worker.status = "errored"
            self._save_state()
            raise RuntimeError(f"Pulumi did not return an IP for {name}")

        worker.ip = ip
        worker.status = "provisioning"
        self._save_state()

        # 2. Wait for SSH
        console.print(f"Waiting for SSH on {ip}...")
        wait_for_ssh(ip, effective_ssh_user, str(self.config.resolve_ssh_key()))

        # 3. Run Ansible bootstrap
        console.print(f"Provisioning [bold]{name}[/bold]...")
        repo_configs = [
            r.model_dump() for r in self.config.repos if r.name in repo_names
        ]
        bootstrap_worker(
            ip=ip,
            worker_name=name,
            model=model,
            repos=repo_configs,
            fleet_config=self.config,
        )

        # 4. Mark idle
        worker.status = "idle"
        self._save_state()
        console.print(f"[green]Worker {name} ready at {ip}[/green]")
        return worker

    # ------------------------------------------------------------------
    # kill
    # ------------------------------------------------------------------

    def kill(self, name: str, collect_path: str | None = None, force: bool = False) -> None:
        """Destroy a worker (VM or container)."""
        if name not in self.state.workers and not force:
            raise KeyError(f"Worker '{name}' not found.")

        worker = self.state.workers.get(name)

        # Optionally collect first
        if collect_path and worker:
            has_target = worker.ip or worker.container_id
            if has_target:
                console.print(f"Collecting from {name}...")
                self.collect(name, collect_path)

        console.print(f"Destroying [bold]{name}[/bold]...")

        if worker and worker.provider == "devcontainer":
            from cfleet.devcontainer import kill_container
            try:
                kill_container(name)
            except Exception as e:
                if not force:
                    raise
                console.print(f"[yellow]Warning: Container removal failed: {e}[/yellow]")
        else:
            try:
                self.infra.remove_worker(name)
            except Exception as e:
                if not force:
                    raise
                console.print(f"[yellow]Warning: Pulumi destroy failed: {e}[/yellow]")

        self.state.remove_worker(name)
        self._save_state()
        console.print(f"[green]Worker {name} destroyed.[/green]")

    def kill_all(self, collect_path: str | None = None) -> None:
        """Kill all workers."""
        names = list(self.state.workers.keys())
        for name in names:
            dest = f"{collect_path}/{name}" if collect_path else None
            self.kill(name, collect_path=dest, force=True)

    # ------------------------------------------------------------------
    # ask
    # ------------------------------------------------------------------

    def ask(self, name: str, prompt: str) -> None:
        """Send a prompt to a worker. Fire and forget."""
        worker = self.state.get_worker(name)
        conn = self._get_conn(worker)
        conn.send_prompt(prompt)
        conn.close()

        worker.last_prompt = prompt
        worker.last_prompt_at = datetime.now(timezone.utc).isoformat()
        worker.status = "working"
        self._save_state()
        console.print(f"Prompt sent to [bold]{name}[/bold].")

    # ------------------------------------------------------------------
    # attach
    # ------------------------------------------------------------------

    def attach(self, name: str) -> None:
        """Attach to a worker's tmux session. Replaces current process."""
        worker = self.state.get_worker(name)
        conn = self._get_conn(worker)
        conn.attach()  # Does not return — replaces process

    # ------------------------------------------------------------------
    # send / collect
    # ------------------------------------------------------------------

    def send(self, name: str, local_path: str, remote_path: str = "/workspace/inbox/") -> None:
        """Send files to a worker."""
        worker = self.state.get_worker(name)
        conn = self._get_conn(worker)
        conn.send_files(local_path, remote_path)
        conn.close()
        console.print(f"Sent {local_path} to [bold]{name}[/bold]:{remote_path}")

    def collect(self, name: str, local_dest: str, remote_path: str = "/workspace/outbox/") -> None:
        """Collect files from a worker."""
        worker = self.state.get_worker(name)
        conn = self._get_conn(worker)
        conn.collect(remote_path, local_dest)
        conn.close()
        console.print(f"Collected {remote_path} from [bold]{name}[/bold] to {local_dest}")

    # ------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------

    def logs(self, name: str, lines: int = 100, follow: bool = False) -> None:
        """Print worker tmux logs."""
        worker = self.state.get_worker(name)
        conn = self._get_conn(worker)

        if follow:
            try:
                for line in conn.stream_logs():
                    console.print(line)
            except KeyboardInterrupt:
                pass
        else:
            output = conn.read_logs(lines=lines)
            console.print(output)

        conn.close()

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self, name: str) -> dict:
        """Get detailed status for a worker."""
        worker = self.state.get_worker(name)
        info = worker.model_dump()

        reachable = (worker.ip or worker.container_id) and worker.status not in ("stopped", "spawning")
        if reachable:
            try:
                conn = self._get_conn(worker)
                info["tmux_alive"] = conn.is_alive()
                stdout, _, _ = conn.exec("uptime -p")
                info["uptime"] = stdout.strip()
                conn.close()
            except Exception:
                info["tmux_alive"] = False
                info["uptime"] = "unknown"
        else:
            info["tmux_alive"] = False
            info["uptime"] = "N/A"

        return info

    # ------------------------------------------------------------------
    # ls (data only — formatting is in CLI)
    # ------------------------------------------------------------------

    def list_workers(self) -> list[WorkerState]:
        """Return all workers."""
        return list(self.state.workers.values())
