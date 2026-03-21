"""Pulumi automation API wrapper with provider factory pattern."""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path

from pulumi import automation as auto

from cfleet.config import FleetConfig, FleetState
from cfleet.infra_pulumi.__main__ import pulumi_program


class CloudProvider(ABC):
    """Base class for cloud provider infrastructure.

    Subclass this and implement the abstract methods to add support for a new
    cloud provider (AWS, GCP, etc.).  The Pulumi program in infra/__main__.py
    reads a 'provider' config key and delegates to the appropriate provider code.
    """

    @abstractmethod
    def get_pulumi_config(self, fleet_config: FleetConfig) -> dict[str, str]:
        """Return Pulumi config key/value pairs for this provider."""

    @abstractmethod
    def get_required_plugins(self) -> list[tuple[str, str]]:
        """Return list of (plugin_name, version) required by this provider."""

    @abstractmethod
    def get_worker_stack_config(self, fleet_config: FleetConfig) -> tuple[str, dict]:
        """Return (config_key, config_dict) for the Pulumi stack."""


class AzureProvider(CloudProvider):
    """Azure provider — uses pulumi-azure-native."""

    def get_pulumi_config(self, fleet_config: FleetConfig) -> dict[str, str]:
        azure = fleet_config.cloud.azure
        return {
            "azure-native:location": fleet_config.resolve_region(provider="azure"),
            "azure-native:subscriptionId": azure.subscription_id,
        }

    def get_required_plugins(self) -> list[tuple[str, str]]:
        return []

    def get_worker_stack_config(self, fleet_config: FleetConfig) -> tuple[str, dict]:
        azure = fleet_config.cloud.azure
        cfg = {
            "resource_group": azure.resource_group,
            "region": fleet_config.resolve_region(provider="azure"),
            "instance_type": fleet_config.resolve_instance_type(provider="azure"),
            "ssh_user": fleet_config.resolve_ssh_user(provider="azure"),
            "ssh_key": str(fleet_config.resolve_ssh_key()),
            "image": azure.image.model_dump(),
        }
        return "claude-fleet:azure", cfg


class GcpProvider(CloudProvider):
    """GCP provider — uses pulumi-gcp."""

    def get_pulumi_config(self, fleet_config: FleetConfig) -> dict[str, str]:
        gcp = fleet_config.cloud.gcp
        return {
            "gcp:project": gcp.project_id,
            "gcp:region": fleet_config.resolve_region(provider="gcp"),
            "gcp:zone": gcp.zone,
        }

    def get_required_plugins(self) -> list[tuple[str, str]]:
        return []

    def get_worker_stack_config(self, fleet_config: FleetConfig) -> tuple[str, dict]:
        gcp = fleet_config.cloud.gcp
        cfg = {
            "project_id": gcp.project_id,
            "region": fleet_config.resolve_region(provider="gcp"),
            "zone": gcp.zone,
            "instance_type": fleet_config.resolve_instance_type(provider="gcp"),
            "ssh_user": fleet_config.resolve_ssh_user(provider="gcp"),
            "ssh_key": str(fleet_config.resolve_ssh_key()),
            "image": gcp.image.model_dump(),
        }
        return "claude-fleet:gcp", cfg


# Provider registry — add new providers here
PROVIDERS: dict[str, type[CloudProvider]] = {
    "azure": AzureProvider,
    "gcp": GcpProvider,
}


def get_provider(name: str) -> CloudProvider:
    cls = PROVIDERS.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown cloud provider '{name}'. Available: {', '.join(PROVIDERS)}"
        )
    return cls()


class InfraManager:
    """Manages Pulumi stack for fleet worker VMs."""

    def __init__(self, fleet_config: FleetConfig):
        self.config = fleet_config

    def _get_stack(self) -> auto.Stack:
        project_name = self.config.pulumi.project
        stack_name = self.config.pulumi.stack
        backend_url = self.config.pulumi.backend.replace("~", str(Path.home()))
        # Ensure backend dir exists for file:// backends
        if backend_url.startswith("file://"):
            Path(backend_url.removeprefix("file://")).mkdir(parents=True, exist_ok=True)

        env_vars = {**os.environ}
        # Default to empty passphrase for local-backend secret encryption
        # so developers don't need to configure it manually.
        env_vars.setdefault("PULUMI_CONFIG_PASSPHRASE", "")

        ws_opts = auto.LocalWorkspaceOptions(
            project_settings=auto.ProjectSettings(
                name=project_name,
                runtime="python",
                backend=auto.ProjectBackend(url=backend_url),
            ),
            env_vars=env_vars,
        )

        # Use inline program mode — runs in-process, no subprocess/pip needed
        try:
            stack = auto.select_stack(
                stack_name=stack_name,
                project_name=project_name,
                program=pulumi_program,
                opts=ws_opts,
            )
        except auto.errors.StackNotFoundError:
            stack = auto.create_stack(
                stack_name=stack_name,
                project_name=project_name,
                program=pulumi_program,
                opts=ws_opts,
            )

        # Set provider config for all configured providers
        for provider_name in PROVIDERS:
            provider = get_provider(provider_name)
            try:
                for key, value in provider.get_pulumi_config(self.config).items():
                    stack.set_config(key, auto.ConfigValue(value=value))
            except Exception:
                pass  # Provider not configured, skip

        return stack

    def init_stack(self) -> None:
        """Initialize the Pulumi stack (creates if needed)."""
        self._get_stack()

    def _workers_from_state(self, exclude: str | None = None) -> dict:
        """Build the Pulumi workers map from fleet state (the source of truth).

        Pulumi inline workspaces use temp dirs, so set_config doesn't persist
        between invocations. We always rebuild from fleet state instead.
        """
        state = FleetState.load()
        workers = {}
        for wname, w in state.workers.items():
            if wname == exclude:
                continue
            if w.ip:  # Only include workers that Pulumi actually created
                workers[wname] = {
                    "instance_type": w.instance_type,
                    "vm_type": w.vm_type,
                    "provider": w.provider,
                }
        return workers

    def _apply_config(self, stack: auto.Stack, workers: dict) -> None:
        """Set all Pulumi config from fleet state before every up."""
        stack.set_config(
            "claude-fleet:workers", auto.ConfigValue(value=json.dumps(workers))
        )
        # Set provider configs for all providers in use
        used_providers = {w.get("provider", self.config.cloud.provider) for w in workers.values()}
        for provider_name in used_providers:
            provider = get_provider(provider_name)
            config_key, provider_cfg = provider.get_worker_stack_config(self.config)
            stack.set_config(
                config_key, auto.ConfigValue(value=json.dumps(provider_cfg))
            )

    def add_worker(self, name: str, worker_cfg: dict, provider: str | None = None) -> dict:
        """Add a worker to the Pulumi config and run up. Returns outputs."""
        stack = self._get_stack()
        workers = self._workers_from_state()
        workers[name] = worker_cfg

        self._apply_config(stack, workers)

        # Sync Pulumi state with actual cloud resources before deploying
        stack.refresh(on_output=lambda msg: None)

        result = stack.up(on_output=lambda msg: None)
        return {k: v.value for k, v in result.outputs.items()}

    def remove_worker(self, name: str) -> None:
        """Remove a worker from the Pulumi config and run up to destroy it."""
        stack = self._get_stack()
        workers = self._workers_from_state(exclude=name)

        self._apply_config(stack, workers)

        stack.refresh(on_output=lambda msg: None)
        stack.up(on_output=lambda msg: None)

    def destroy_all(self) -> None:
        """Destroy all infrastructure."""
        stack = self._get_stack()
        self._apply_config(stack, {})
        stack.up(on_output=lambda msg: None)
