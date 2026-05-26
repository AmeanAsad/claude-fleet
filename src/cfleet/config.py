"""Pydantic models for fleet config (~/.cfleet/config.yml) and state (~/.cfleet/state.json)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class VMType(str, Enum):
    """VM security type — determines SKU family and confidential compute settings."""
    REGULAR = "regular"
    SNP = "snp"
    TDX = "tdx"


class GitHubLevel(str, Enum):
    """GitHub access level for workers — controls token scoping."""
    NONE = "none"
    READ = "read"
    TRIAGE = "triage"
    WRITE = "write"


GH_PERMISSION_MAP: dict[GitHubLevel, dict[str, str]] = {
    GitHubLevel.NONE: {},
    GitHubLevel.READ: {"contents": "read", "metadata": "read"},
    GitHubLevel.TRIAGE: {"contents": "read", "metadata": "read", "issues": "write"},
    GitHubLevel.WRITE: {
        "contents": "write",
        "metadata": "read",
        "issues": "write",
        "pull_requests": "write",
    },
}


# Providers that require cloud infra (Pulumi + SSH)
CLOUD_PROVIDERS = {"azure", "gcp"}

# Default SKUs per provider and VM type
DEFAULT_SKUS: dict[str, dict[VMType, str]] = {
    "azure": {
        VMType.REGULAR: "Standard_D2s_v5",
        VMType.SNP: "Standard_DC4as_v5",
        VMType.TDX: "Standard_DC4es_v6",
    },
    "gcp": {
        VMType.REGULAR: "e2-standard-2",
        VMType.SNP: "n2d-standard-2",
        VMType.TDX: "c3-standard-4",
    },
}

# Provider-specific defaults for fields that live on CloudConfig
PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "azure": {
        "region": "westeurope",
        "ssh_user": "ubuntu",
        "instance_type": "Standard_D2s_v5",
    },
    "gcp": {
        "region": "us-central1",
        "ssh_user": "ubuntu",
        "instance_type": "e2-standard-2",
    },
    "devcontainer": {
        "region": "local",
        "ssh_user": "vscode",
        "instance_type": "docker",
    },
}


FLEET_DIR = Path.home() / ".cfleet"
CONFIG_PATH = FLEET_DIR / "config.yml"
STATE_PATH = FLEET_DIR / "state.json"


# ---------------------------------------------------------------------------
# Config models (config.yml)
# ---------------------------------------------------------------------------


class RepoConfig(BaseModel):
    name: str
    url: str
    branch: str = "main"


class AzureImageConfig(BaseModel):
    publisher: str = "Canonical"
    offer: str = "ubuntu-24_04-lts"
    sku: str = "server"
    version: str = "latest"


class AzureConfig(BaseModel):
    subscription_id: str = ""
    resource_group: str = ""  # generated with unique slug during init
    image: AzureImageConfig = AzureImageConfig()
    vnet: Optional[str] = None
    subnet: Optional[str] = None


class GcpImageConfig(BaseModel):
    project: str = "ubuntu-os-cloud"
    family: str = "ubuntu-2404-lts-amd64"


class GcpConfig(BaseModel):
    project_id: str = ""
    zone: str = "us-central1-a"
    image: GcpImageConfig = GcpImageConfig()


class CloudConfig(BaseModel):
    provider: str = ""
    region: str = ""
    vm_type: VMType = VMType.REGULAR
    instance_type: str = ""
    ssh_key: str = "~/.ssh/id_ed25519"
    ssh_user: str = ""
    azure: AzureConfig = AzureConfig()
    gcp: GcpConfig = GcpConfig()


class GitHubConfig(BaseModel):
    """GitHub App credentials for token brokering."""
    app_id: str = ""
    installation_id: str = ""
    private_key_path: str = "~/.cfleet/github-app.pem"

    def resolve_private_key_path(self) -> Path:
        return Path(self.private_key_path).expanduser()

    def is_configured(self) -> bool:
        return bool(self.app_id and self.installation_id)


class ServerConfig(BaseModel):
    """Central server config — used by both server and clients."""
    url: str = ""  # e.g. http://my-server:8420 — set via `cfleet connect`
    host: str = "0.0.0.0"
    port: int = 8420
    token: str = ""  # bearer token for API + worker registration


class PulumiConfig(BaseModel):
    project: str = "claude-fleet"
    stack: str = "default"
    backend: str = "file://~/.cfleet/pulumi-state"


class FleetConfig(BaseModel):
    anthropic_api_key: str = ""
    model: str = "claude-opus-4-6"
    secrets_env: str = "~/.cfleet/secrets.env"
    repos: list[RepoConfig] = Field(default_factory=list)
    skills_dir: str = "~/.cfleet/skills/"
    claude_md: str = "~/.cfleet/CLAUDE.md"
    mcp_config: str = "~/.cfleet/mcp-servers.json"
    worker_relay_port: int = 8421
    pulumi: PulumiConfig = PulumiConfig()
    cloud: CloudConfig = CloudConfig()
    server: ServerConfig = ServerConfig()
    github: GitHubConfig = GitHubConfig()

    @classmethod
    def load(cls, path: Path | None = None) -> FleetConfig:
        p = path or CONFIG_PATH
        if not p.exists():
            raise FileNotFoundError(f"Config not found at {p}. Run 'cfleet init' first.")
        raw = yaml.safe_load(p.read_text()) or {}
        return cls.model_validate(raw)

    def save(self, path: Path | None = None) -> None:
        p = path or CONFIG_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.dump(self.model_dump(mode="json"), default_flow_style=False, sort_keys=False))

    def resolve_provider_default(self, field: str) -> str:
        """Get a provider-appropriate default for a CloudConfig field."""
        defaults = PROVIDER_DEFAULTS.get(self.cloud.provider, {})
        return defaults.get(field, "")

    def resolve_ssh_user(self, provider: str | None = None) -> str:
        p = provider or self.cloud.provider
        # Global overrides only apply to the default provider
        if self.cloud.ssh_user and p == self.cloud.provider:
            return self.cloud.ssh_user
        return PROVIDER_DEFAULTS.get(p, {}).get("ssh_user", "")

    def resolve_region(self, provider: str | None = None) -> str:
        p = provider or self.cloud.provider
        if self.cloud.region and p == self.cloud.provider:
            return self.cloud.region
        return PROVIDER_DEFAULTS.get(p, {}).get("region", "")

    def resolve_instance_type(self, provider: str | None = None, vm_type: VMType | None = None) -> str:
        p = provider or self.cloud.provider
        vt = vm_type or self.cloud.vm_type
        if self.cloud.instance_type and p == self.cloud.provider:
            return self.cloud.instance_type
        skus = DEFAULT_SKUS.get(p, {})
        return skus.get(vt, "")

    def resolve_ssh_key(self) -> Path:
        return Path(self.cloud.ssh_key).expanduser()

    def resolve_secrets_env(self) -> Path:
        return Path(self.secrets_env).expanduser()

    def resolve_skills_dir(self) -> Path:
        return Path(self.skills_dir).expanduser()

    def resolve_claude_md(self) -> Path:
        return Path(self.claude_md).expanduser()

    def resolve_mcp_config(self) -> Path:
        return Path(self.mcp_config).expanduser()


# ---------------------------------------------------------------------------
# State models (state.json)
# ---------------------------------------------------------------------------


class MachineState(BaseModel):
    name: str
    provider: str = ""  # azure | gcp | devcontainer | external
    ip: str = ""
    region: str = ""
    instance_type: str = ""
    vm_type: str = "regular"  # regular | snp | tdx
    ssh_user: str = ""
    container_id: str = ""  # devcontainer only
    hostname: str = ""  # external only — reported by the machine agent
    os_info: str = ""  # external only — e.g. "Ubuntu 24.04"
    status: str = "creating"  # creating | provisioning | ready | errored | stopped | disconnected
    worker_names: list[str] = Field(default_factory=list)
    next_relay_port: int = 8421
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class WorkerState(BaseModel):
    name: str
    machine_name: str = ""
    relay_port: int = 8421
    model: str = ""
    repos: list[str] = Field(default_factory=list)
    status: str = "spawning"  # spawning | provisioning | idle | working | errored | stopped
    session_id: Optional[str] = None
    github_level: str = "none"  # none | read | triage | write
    local_mode: bool = False  # True when started via `cfleet agent`
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_prompt: Optional[str] = None
    last_prompt_at: Optional[str] = None


class GitHubTokenLog(BaseModel):
    worker_name: str
    level: str
    repos: list[str] = Field(default_factory=list)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: str = ""


class FleetState(BaseModel):
    machines: dict[str, MachineState] = Field(default_factory=dict)
    workers: dict[str, WorkerState] = Field(default_factory=dict)
    github_token_log: list[GitHubTokenLog] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> FleetState:
        p = path or STATE_PATH
        if not p.exists():
            return cls()
        raw = json.loads(p.read_text()) or {}
        return cls.model_validate(raw)

    def save(self, path: Path | None = None) -> None:
        p = path or STATE_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.model_dump(), indent=2) + "\n")

    # -- Machine helpers -------------------------------------------------------

    def get_machine(self, name: str) -> MachineState:
        if name not in self.machines:
            raise KeyError(f"Machine '{name}' not found. Run 'cfleet machine ls' to see machines.")
        return self.machines[name]

    def add_machine(self, machine: MachineState) -> None:
        self.machines[machine.name] = machine

    def remove_machine(self, name: str) -> None:
        machine = self.machines.pop(name, None)
        if machine:
            for wname in list(machine.worker_names):
                self.workers.pop(wname, None)

    def allocate_relay_port(self, machine_name: str) -> int:
        machine = self.get_machine(machine_name)
        port = machine.next_relay_port
        machine.next_relay_port = port + 1
        return port

    def get_workers_on_machine(self, machine_name: str) -> list[WorkerState]:
        return [w for w in self.workers.values() if w.machine_name == machine_name]

    # -- Worker helpers --------------------------------------------------------

    def get_worker(self, name: str) -> WorkerState:
        if name not in self.workers:
            raise KeyError(f"Worker '{name}' not found. Run 'cfleet ls' to see workers.")
        return self.workers[name]

    def add_worker(self, worker: WorkerState) -> None:
        self.workers[worker.name] = worker
        if worker.machine_name and worker.machine_name in self.machines:
            machine = self.machines[worker.machine_name]
            if worker.name not in machine.worker_names:
                machine.worker_names.append(worker.name)

    def remove_worker(self, name: str) -> None:
        worker = self.workers.pop(name, None)
        if worker and worker.machine_name in self.machines:
            machine = self.machines[worker.machine_name]
            if name in machine.worker_names:
                machine.worker_names.remove(name)
