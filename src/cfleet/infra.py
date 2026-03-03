"""Pulumi automation API wrapper with provider factory pattern."""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path

from pulumi import automation as auto

from cfleet.config import FleetConfig


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


class AzureProvider(CloudProvider):
    """Azure provider — uses pulumi-azure-native."""

    def get_pulumi_config(self, fleet_config: FleetConfig) -> dict[str, str]:
        azure = fleet_config.cloud.azure
        return {
            "azure-native:location": fleet_config.cloud.region,
            "azure-native:subscriptionId": azure.subscription_id,
        }

    def get_required_plugins(self) -> list[tuple[str, str]]:
        # Don't pin version — let Pulumi resolve
        return []


# Provider registry — add new providers here
PROVIDERS: dict[str, type[CloudProvider]] = {
    "azure": AzureProvider,
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
        self.provider = get_provider(fleet_config.cloud.provider)
        self._infra_dir = Path(__file__).parent.parent.parent / "infra"

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

        try:
            stack = auto.select_stack(
                stack_name=stack_name,
                work_dir=str(self._infra_dir),
                opts=auto.LocalWorkspaceOptions(
                    project_settings=auto.ProjectSettings(
                        name=project_name,
                        runtime="python",
                        backend=auto.ProjectBackend(url=backend_url),
                    ),
                    env_vars=env_vars,
                ),
            )
        except auto.errors.StackNotFoundError:
            stack = auto.create_stack(
                stack_name=stack_name,
                work_dir=str(self._infra_dir),
                opts=auto.LocalWorkspaceOptions(
                    project_settings=auto.ProjectSettings(
                        name=project_name,
                        runtime="python",
                        backend=auto.ProjectBackend(url=backend_url),
                    ),
                    env_vars=env_vars,
                ),
            )

        # Set provider-specific config
        for key, value in self.provider.get_pulumi_config(self.config).items():
            stack.set_config(key, auto.ConfigValue(value=value))

        return stack

    def init_stack(self) -> None:
        """Initialize the Pulumi stack (creates if needed)."""
        stack = self._get_stack()
        # Set empty workers map if not present
        try:
            stack.get_config("claude-fleet:workers")
        except auto.errors.CommandError:
            stack.set_config(
                "claude-fleet:workers", auto.ConfigValue(value=json.dumps({}))
            )

    def add_worker(self, name: str, worker_cfg: dict) -> dict:
        """Add a worker to the Pulumi config and run up. Returns outputs."""
        stack = self._get_stack()
        try:
            workers_raw = stack.get_config("claude-fleet:workers")
            workers = json.loads(workers_raw.value)
        except (auto.errors.CommandError, KeyError):
            workers = {}

        workers[name] = worker_cfg
        stack.set_config(
            "claude-fleet:workers", auto.ConfigValue(value=json.dumps(workers))
        )

        # Set azure-specific config for the Pulumi program
        azure = self.config.cloud.azure
        azure_cfg = {
            "resource_group": azure.resource_group,
            "region": self.config.cloud.region,
            "instance_type": self.config.cloud.instance_type,
            "ssh_user": self.config.cloud.ssh_user,
            "ssh_key": str(self.config.resolve_ssh_key()),
            "image": azure.image.model_dump(),
        }
        stack.set_config(
            "claude-fleet:azure", auto.ConfigValue(value=json.dumps(azure_cfg))
        )

        result = stack.up(on_output=lambda msg: None)
        return {k: v.value for k, v in result.outputs.items()}

    def remove_worker(self, name: str) -> None:
        """Remove a worker from the Pulumi config and run up to destroy it."""
        stack = self._get_stack()
        try:
            workers_raw = stack.get_config("claude-fleet:workers")
            workers = json.loads(workers_raw.value)
        except (auto.errors.CommandError, KeyError):
            return

        workers.pop(name, None)
        stack.set_config(
            "claude-fleet:workers", auto.ConfigValue(value=json.dumps(workers))
        )

        stack.up(on_output=lambda msg: None)

    def destroy_all(self) -> None:
        """Destroy all infrastructure."""
        stack = self._get_stack()
        stack.set_config(
            "claude-fleet:workers", auto.ConfigValue(value=json.dumps({}))
        )
        stack.up(on_output=lambda msg: None)
