"""Core orchestration engine — ties infra, provisioner, server API, and SSH together.

Command/query operations (ask, logs, status, interrupt, messages) go through
the central server API. SSH is used only for provisioning, file transfer, and
interactive attach.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
from rich.console import Console

from cfleet.config import (
    CLOUD_PROVIDERS,
    FleetConfig,
    FleetState,
    MachineState,
    VMType,
    WorkerState,
)
from cfleet.relay_client import format_message

console = Console()


class FleetEngine:
    """Main orchestration entry point. All CLI/TUI commands go through here."""

    def __init__(self, config: FleetConfig | None = None, state: FleetState | None = None):
        self.config = config or FleetConfig.load()
        self.state = state or FleetState.load()
        self._infra = None

    @property
    def infra(self):
        if self._infra is None:
            from cfleet.infra import InfraManager
            self._infra = InfraManager(self.config)
        return self._infra

    def _save_state(self) -> None:
        self.state.save()

    def _get_machine_for_worker(self, worker: WorkerState) -> MachineState:
        return self.state.get_machine(worker.machine_name)

    # ------------------------------------------------------------------
    # Server API client
    # ------------------------------------------------------------------

    def _server_url(self) -> str:
        url = self.config.server.url
        if not url:
            host = self.config.server.host
            port = self.config.server.port
            url = f"http://{host}:{port}"
        return url.rstrip("/")

    def _server_headers(self) -> dict:
        headers = {}
        if self.config.server.token:
            headers["Authorization"] = f"Bearer {self.config.server.token}"
        return headers

    def _api_get(self, path: str, **params) -> dict:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(
                f"{self._server_url()}{path}",
                headers=self._server_headers(),
                params=params,
            )
            r.raise_for_status()
            return r.json()

    def _api_post(self, path: str, json_body: dict | None = None) -> dict:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                f"{self._server_url()}{path}",
                headers=self._server_headers(),
                json=json_body,
            )
            r.raise_for_status()
            return r.json()

    def _api_delete(self, path: str) -> dict:
        with httpx.Client(timeout=30.0) as client:
            r = client.delete(
                f"{self._server_url()}{path}",
                headers=self._server_headers(),
            )
            r.raise_for_status()
            return r.json()

    # ------------------------------------------------------------------
    # init
    # ------------------------------------------------------------------

    def init(self) -> None:
        """Initialize ~/.cfleet/ directory and Pulumi stack (if cloud provider)."""
        from cfleet.config import FLEET_DIR, CONFIG_PATH

        FLEET_DIR.mkdir(parents=True, exist_ok=True)
        (FLEET_DIR / "skills").mkdir(exist_ok=True)

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
    # validation
    # ------------------------------------------------------------------

    def _validate_config(self, provider: str | None = None) -> None:
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
        elif effective_provider in ("devcontainer", "external"):
            pass
        if effective_provider in CLOUD_PROVIDERS:
            if not self.config.resolve_ssh_user(provider=effective_provider):
                missing.append("cloud.ssh_user")
        if missing:
            raise ValueError(
                f"Missing required config: {', '.join(missing)}. "
                "Run 'cfleet init' to set them."
            )

    # ------------------------------------------------------------------
    # machine lifecycle
    # ------------------------------------------------------------------

    def create_machine(
        self,
        name: str,
        provider: str | None = None,
        vm_type: VMType | None = None,
        instance_type: str | None = None,
        region: str | None = None,
    ) -> MachineState:
        """Create a new machine (cloud VM or devcontainer host)."""
        provider_name = provider or self.config.cloud.provider
        self._validate_config(provider=provider_name)

        if name in self.state.machines:
            raise ValueError(f"Machine '{name}' already exists.")

        effective_vm_type = vm_type or self.config.cloud.vm_type
        effective_instance_type = instance_type or self.config.resolve_instance_type(
            provider=provider_name, vm_type=effective_vm_type
        )
        effective_ssh_user = self.config.resolve_ssh_user(provider=provider_name)

        machine = MachineState(
            name=name,
            provider=provider_name,
            vm_type=effective_vm_type.value,
            instance_type=effective_instance_type,
            ssh_user=effective_ssh_user,
            status="creating",
        )

        if provider_name == "devcontainer":
            machine.status = "ready"
            self.state.add_machine(machine)
            self._save_state()
            console.print(f"[green]Machine {name} ready (devcontainer)[/green]")
            return machine

        self.state.add_machine(machine)
        self._save_state()

        console.print(
            f"[bold]{provider_name}[/bold] | "
            f"VM type: [bold]{effective_vm_type.value}[/bold] | "
            f"SKU: [bold]{effective_instance_type}[/bold]"
        )

        try:
            console.print(f"Creating VM [bold]{name}[/bold]...")
            machine_cfg = {
                "instance_type": effective_instance_type,
                "vm_type": effective_vm_type.value,
                "provider": provider_name,
            }
            if region:
                if provider_name == "gcp":
                    machine_cfg["zone"] = region
                else:
                    machine_cfg["region"] = region
                machine.region = region

            outputs = self.infra.add_machine(name, machine_cfg)
            ip = outputs.get(f"{name}_ip", "")
            if not ip:
                raise RuntimeError(f"Pulumi did not return an IP for {name}")

            machine.ip = ip
            machine.status = "provisioning"
            self._save_state()

            from cfleet.ssh import wait_for_ssh
            console.print(f"Waiting for SSH on {ip}...")
            wait_for_ssh(ip, effective_ssh_user, str(self.config.resolve_ssh_key()))

            from cfleet.provisioner import bootstrap_machine
            console.print(f"Bootstrapping [bold]{name}[/bold]...")
            bootstrap_machine(
                ip=ip,
                user=effective_ssh_user,
                key_path=str(self.config.resolve_ssh_key()),
                fleet_config=self.config,
                machine_name=name,
            )

            machine.status = "ready"
            self._save_state()
            console.print(f"[green]Machine {name} ready at {ip}[/green]")
            return machine
        except Exception:
            # Roll the cfleet record back so the user can retry without `--purge`.
            # The Pulumi side may have partial resources; surface that to the user
            # but don't block retries on a stale cfleet record.
            console.print(
                f"[yellow]Create failed — rolling back cfleet record for '{name}'. "
                f"If Pulumi created resources, run `cfleet machine doctor` (coming) "
                f"or clean up manually via the Pulumi state.[/yellow]"
            )
            try:
                self.state.remove_machine(name)
                self._save_state()
            except Exception:
                pass
            raise

    def remove_machine(self, name: str, keep_vm: bool = False, purge: bool = False) -> None:
        """Remove a machine and all its workers. Always removes from state.

        purge=True skips all remote cleanup (Pulumi, Docker, SSH).
        keep_vm=True removes from state but leaves the cloud VM intact.
        """
        machine = self.state.get_machine(name)

        for wname in list(machine.worker_names):
            self.kill(wname, force=True, purge=purge)

        if not purge and not keep_vm:
            if machine.provider in ("devcontainer", "external"):
                pass
            else:
                console.print(f"Destroying VM [bold]{name}[/bold]...")
                try:
                    self.infra.remove_machine(name)
                except Exception as e:
                    console.print(f"[yellow]Warning: Pulumi destroy failed: {e}[/yellow]")

        self.state.remove_machine(name)
        self._save_state()
        console.print(f"[green]Machine {name} removed.[/green]")

    def list_machines(self) -> list[MachineState]:
        return list(self.state.machines.values())

    # ------------------------------------------------------------------
    # spawn (worker on a machine)
    # ------------------------------------------------------------------

    def spawn(
        self,
        name: str,
        machine_name: str | None = None,
        repos: list[str] | None = None,
        model: str | None = None,
        provider: str | None = None,
        vm_type: VMType | None = None,
        instance_type: str | None = None,
        region: str | None = None,
        cwd: str | None = None,
    ) -> WorkerState:
        """Spawn a new worker on a machine. Auto-creates machine if needed."""
        if name in self.state.workers:
            raise ValueError(f"Worker '{name}' already exists. Kill it first or choose a different name.")

        effective_model = model or self.config.model
        effective_repos = repos or [r.name for r in self.config.repos]

        if machine_name and machine_name in self.state.machines:
            machine = self.state.get_machine(machine_name)
            if machine.status != "ready":
                raise ValueError(f"Machine '{machine_name}' is not ready (status: {machine.status})")
        else:
            auto_name = machine_name or f"machine-{name}"
            if auto_name in self.state.machines:
                machine = self.state.get_machine(auto_name)
            else:
                console.print(f"Auto-creating machine [bold]{auto_name}[/bold]...")
                machine = self.create_machine(
                    auto_name,
                    provider=provider,
                    vm_type=vm_type,
                    instance_type=instance_type,
                    region=region,
                )
            machine_name = auto_name

        relay_port = self.state.allocate_relay_port(machine.name)

        worker = WorkerState(
            name=name,
            machine_name=machine.name,
            relay_port=relay_port,
            model=effective_model,
            repos=effective_repos,
            status="spawning",
        )
        self.state.add_worker(worker)
        self._save_state()

        repo_configs = [r.model_dump() for r in self.config.repos if r.name in effective_repos]

        if machine.provider == "devcontainer":
            self._spawn_worker_devcontainer(worker, machine, effective_model, repo_configs)
        else:
            # All other machines (external + cloud) self-register their
            # machine-agent, so spawning goes through the same WS path.
            self._spawn_worker_external(worker, machine, effective_model, repo_configs, cwd=cwd)

        worker.status = "idle"
        self._save_state()
        console.print(f"[green]Worker {name} ready on {machine.name}[/green]")
        return worker

    def _spawn_worker_devcontainer(
        self, worker: WorkerState, machine: MachineState, model: str, repos: list[dict]
    ) -> None:
        raise RuntimeError(
            "The devcontainer worker path is currently unsupported — it still "
            "depends on the removed worker_relay.py runtime. Use a registered "
            "external/cloud machine instead, or open an issue if you need "
            "devcontainer support brought up to parity."
        )
        from cfleet.devcontainer import docker_available, spawn_container

        if not docker_available():
            raise RuntimeError("Docker is not available.")

        console.print(f"[bold]devcontainer[/bold] | Spawning container [bold]{worker.name}[/bold]...")
        container_id = spawn_container(
            name=worker.name,
            anthropic_api_key=self.config.anthropic_api_key,
            model=model,
            repos=repos,
            fleet_config=self.config,
        )
        machine.container_id = container_id
        self._save_state()

    def _spawn_worker_external(
        self, worker: WorkerState, machine: MachineState, model: str, repos: list[dict],
        cwd: str | None = None,
    ) -> None:
        console.print(f"Sending spawn to external machine [bold]{machine.name}[/bold]...")
        payload: dict = {
            "worker_name": worker.name,
            "model": model,
            "repos": repos,
        }
        if cwd:
            payload["cwd"] = cwd
        result = self._api_post(f"/api/machines/{machine.name}/spawn", payload)
        if "error" in result:
            raise RuntimeError(f"Remote spawn failed: {result['error']}")

    # ------------------------------------------------------------------
    # kill (worker)
    # ------------------------------------------------------------------

    def kill(
        self,
        name: str,
        collect_path: str | None = None,
        force: bool = False,
        remove_machine: bool = False,
        purge: bool = False,
    ) -> None:
        """Kill a worker. Always removes from state — cleanup failures are warnings.

        purge=True skips all remote cleanup (useful when machine is already gone).
        force=True allows killing a worker not in state.
        """
        if name not in self.state.workers and not force:
            raise KeyError(f"Worker '{name}' not found.")

        worker = self.state.workers.get(name)

        if not purge and collect_path and worker:
            machine = self.state.machines.get(worker.machine_name)
            if machine and (machine.ip or machine.container_id):
                try:
                    console.print(f"Collecting from {name}...")
                    self.collect(name, collect_path)
                except Exception as e:
                    console.print(f"[yellow]Warning: Collection failed: {e}[/yellow]")

        console.print(f"Destroying worker [bold]{name}[/bold]...")

        if not purge and worker:
            machine = self.state.machines.get(worker.machine_name)
            if machine:
                if machine.provider == "external":
                    try:
                        self._api_post(f"/api/machines/{machine.name}/kill", {
                            "worker_name": name,
                        })
                    except Exception as e:
                        console.print(f"[yellow]Warning: Failed to kill worker on external machine: {e}[/yellow]")
                elif machine.provider == "devcontainer":
                    from cfleet.devcontainer import kill_container
                    try:
                        kill_container(worker.name)
                    except Exception as e:
                        console.print(f"[yellow]Warning: Container removal failed: {e}[/yellow]")
                else:
                    try:
                        from cfleet.ssh import ssh_run
                        ssh_user = machine.ssh_user or self.config.resolve_ssh_user(provider=machine.provider)
                        ssh_run(
                            machine.ip, ssh_user,
                            str(self.config.resolve_ssh_key()),
                            f"sudo systemctl stop cfleet-relay-{name} 2>/dev/null; "
                            f"sudo systemctl disable cfleet-relay-{name} 2>/dev/null; "
                            f"sudo rm -f /etc/systemd/system/cfleet-relay-{name}.service; "
                            f"sudo systemctl daemon-reload",
                            timeout=30,
                        )
                    except Exception as e:
                        console.print(f"[yellow]Warning: Failed to stop relay service: {e}[/yellow]")

        self.state.remove_worker(name)
        self._save_state()
        console.print(f"[green]Worker {name} destroyed.[/green]")

        if remove_machine and worker and worker.machine_name:
            machine = self.state.machines.get(worker.machine_name)
            if machine and not machine.worker_names:
                self.remove_machine(worker.machine_name)

    def kill_all(self, collect_path: str | None = None) -> None:
        """Kill all workers, then remove all machines."""
        worker_names = list(self.state.workers.keys())
        for wname in worker_names:
            dest = f"{collect_path}/{wname}" if collect_path else None
            self.kill(wname, collect_path=dest, force=True)

        machine_names = list(self.state.machines.keys())
        for mname in machine_names:
            self.remove_machine(mname)

    # ------------------------------------------------------------------
    # ask — via server API
    # ------------------------------------------------------------------

    def ask(self, name: str, prompt: str) -> None:
        """Send a prompt to a worker via the server."""
        self.state.get_worker(name)  # validate exists
        self._api_post(f"/api/workers/{name}/ask", {"prompt": prompt})
        worker = self.state.get_worker(name)
        worker.last_prompt = prompt
        worker.last_prompt_at = datetime.now(timezone.utc).isoformat()
        worker.status = "working"
        self._save_state()
        console.print(f"Prompt sent to [bold]{name}[/bold].")

    # ------------------------------------------------------------------
    # interrupt — via server API
    # ------------------------------------------------------------------

    def interrupt(self, name: str) -> None:
        """Interrupt the current agent run on a worker via the server."""
        self.state.get_worker(name)
        self._api_post(f"/api/workers/{name}/interrupt")
        worker = self.state.get_worker(name)
        worker.status = "idle"
        self._save_state()
        console.print(f"Interrupted [bold]{name}[/bold].")

    # ------------------------------------------------------------------
    # attach — SSH / Docker (not via server)
    # ------------------------------------------------------------------

    def attach(self, name: str) -> None:
        """Attach to a worker's machine shell. Replaces current process."""
        worker = self.state.get_worker(name)
        machine = self._get_machine_for_worker(worker)

        if machine.provider == "devcontainer":
            from cfleet.devcontainer import WorkerDocker
            docker = WorkerDocker(container_id=machine.container_id, relay_port=worker.relay_port)
            docker.attach()
        elif machine.provider == "external":
            console.print(
                f"[yellow]Machine '{machine.name}' is external (joined via 'cfleet join').[/yellow]\n"
                f"[dim]Open a shell on that host directly; worker cwd is the path passed to 'cfleet agent --cwd'.[/dim]"
            )
        else:
            from cfleet.ssh import ssh_attach
            ssh_user = machine.ssh_user or self.config.resolve_ssh_user(provider=machine.provider)
            ssh_attach(machine.ip, ssh_user, str(self.config.resolve_ssh_key()))

    # ------------------------------------------------------------------
    # send / collect — SSH / Docker (not via server)
    # ------------------------------------------------------------------

    def send(self, name: str, local_path: str, remote_path: str = "/workspace/inbox/") -> None:
        """Send files to a worker's machine via rsync."""
        worker = self.state.get_worker(name)
        machine = self._get_machine_for_worker(worker)

        if machine.provider == "devcontainer":
            from cfleet.devcontainer import WorkerDocker
            docker = WorkerDocker(container_id=machine.container_id, relay_port=worker.relay_port)
            docker.send_files(local_path, remote_path)
        elif machine.provider == "external":
            console.print(
                f"[yellow]send/collect not supported for external machines.[/yellow]\n"
                f"[dim]Copy files directly to/from the agent's --cwd on host '{machine.name}'.[/dim]"
            )
            return
        else:
            from cfleet.ssh import rsync_to
            ssh_user = machine.ssh_user or self.config.resolve_ssh_user(provider=machine.provider)
            rsync_to(machine.ip, ssh_user, str(self.config.resolve_ssh_key()), local_path, remote_path)

        console.print(f"Sent {local_path} to [bold]{name}[/bold]:{remote_path}")

    def collect(self, name: str, local_dest: str, remote_path: str = "/workspace/outbox/") -> None:
        """Collect files from a worker's machine via rsync."""
        worker = self.state.get_worker(name)
        machine = self._get_machine_for_worker(worker)

        if machine.provider == "devcontainer":
            from cfleet.devcontainer import WorkerDocker
            docker = WorkerDocker(container_id=machine.container_id, relay_port=worker.relay_port)
            docker.collect(remote_path, local_dest)
        elif machine.provider == "external":
            console.print(
                f"[yellow]send/collect not supported for external machines.[/yellow]\n"
                f"[dim]Copy files directly to/from the agent's --cwd on host '{machine.name}'.[/dim]"
            )
            return
        else:
            from cfleet.ssh import rsync_from
            ssh_user = machine.ssh_user or self.config.resolve_ssh_user(provider=machine.provider)
            rsync_from(machine.ip, ssh_user, str(self.config.resolve_ssh_key()), remote_path, local_dest)

        console.print(f"Collected {remote_path} from [bold]{name}[/bold] to {local_dest}")

    # ------------------------------------------------------------------
    # logs — via server API
    # ------------------------------------------------------------------

    def logs(self, name: str, lines: int = 100, follow: bool = False) -> None:
        """Print worker logs via the server."""
        self.state.get_worker(name)

        if follow:
            try:
                self._stream_logs_sse(name)
            except KeyboardInterrupt:
                pass
        else:
            result = self._api_get(f"/api/workers/{name}/messages", offset=0, limit=lines)
            for msg in result.get("messages", []):
                formatted = format_message(msg)
                if formatted.strip():
                    console.print(formatted, markup=True)

    def _stream_logs_sse(self, name: str) -> None:
        """Stream logs from the server's SSE endpoint."""
        with httpx.Client(timeout=None) as client:
            with client.stream(
                "GET",
                f"{self._server_url()}/api/workers/{name}/logs",
                headers=self._server_headers(),
            ) as response:
                buffer = ""
                event_type = ""
                for chunk in response.iter_text():
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.rstrip("\r")
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data = line[5:].strip()
                            if data and event_type in ("logs", "message"):
                                try:
                                    parsed = __import__("json").loads(data)
                                    if "formatted" in parsed:
                                        console.print(parsed["formatted"], markup=True)
                                    elif "content" in parsed:
                                        console.print(parsed["content"], markup=True)
                                except Exception:
                                    pass
                            event_type = ""
                        elif line == "":
                            event_type = ""

    # ------------------------------------------------------------------
    # status — via server API
    # ------------------------------------------------------------------

    def status(self, name: str) -> dict:
        """Get detailed status for a worker via the server."""
        return self._api_get(f"/api/workers/{name}")

    # ------------------------------------------------------------------
    # messages — via server API
    # ------------------------------------------------------------------

    def messages(self, name: str, offset: int = 0, limit: int = 200) -> dict:
        """Get structured conversation history via the server."""
        return self._api_get(f"/api/workers/{name}/messages", offset=offset, limit=limit)

    # ------------------------------------------------------------------
    # ls
    # ------------------------------------------------------------------

    def list_workers(self) -> list[WorkerState]:
        return list(self.state.workers.values())
