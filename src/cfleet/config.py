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
    GitHubLevel.READ: {
        "contents": "read",
        "metadata": "read",
        "packages": "read",
    },
    GitHubLevel.TRIAGE: {
        "contents": "read",
        "metadata": "read",
        "packages": "read",
        "issues": "write",
    },
    GitHubLevel.WRITE: {
        "contents": "write",
        "metadata": "read",
        "packages": "read",
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


class OperatorKey(BaseModel):
    """Per-operator API key, hashed at rest.

    Each operator (laptop, ipad, CI) gets its own key. Authenticates only
    the high-trust /api/admin/* and /api/config/secrets endpoints; revocable
    individually without affecting workers or other operators.
    """
    name: str
    key_hash: str  # sha256 hex of the issued key
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ServerConfig(BaseModel):
    """Central server config — used by both server and clients.

    Holds three credential types:
      - `token` (legacy): single shared bearer; honored as both operator AND
        joiner for backwards compat during migration.
      - `joiner_token`: new dedicated joiner credential — machines + workers
        present this on `/ws/machine`, `/ws/worker`, `/api/github/token`,
        and `/api/config/bootstrap`.
      - `operator_keys`: list of named keys, each individually rotatable;
        required for `/api/admin/*` and `/api/config/secrets`.

    On the client side the same struct is used to hold the URL + whichever
    one of the credentials this client was issued.
    """
    url: str = ""  # e.g. http://my-server:8420 — set via `cfleet connect`
    host: str = "0.0.0.0"
    port: int = 8420
    token: str = ""  # legacy single token; still accepted server-side
    joiner_token: str = ""  # new joiner credential
    operator_keys: list[OperatorKey] = Field(default_factory=list)  # server-side roster
    operator_key: str = ""  # client-side: the operator key this host was issued


class SecretsConfig(BaseModel):
    """Canonical secret store. Lives on the central server; distributed to
    joiners via /api/config/bootstrap and to operators via /api/config/secrets.

    On a fresh client this is empty until the client connects and pulls.
    """
    anthropic_api_key: str = ""
    kimi_api_key: str = ""
    model: str = ""  # default model joiners adopt (falls back to top-level FleetConfig.model)


class PulumiConfig(BaseModel):
    project: str = "claude-fleet"
    stack: str = "default"
    backend: str = "file://~/.cfleet/pulumi-state"


class FleetConfig(BaseModel):
    anthropic_api_key: str = ""  # legacy: prefer secrets.anthropic_api_key
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
    secrets: SecretsConfig = SecretsConfig()
    repo_url: str = "https://github.com/AmeanAsad/claude-fleet.git"
    repo_branch: str = "fleat/v2-fleet"

    def resolve_anthropic_key(self) -> str:
        """Return the canonical Anthropic API key.

        Prefers the new `secrets.anthropic_api_key` field; falls back to the
        legacy top-level `anthropic_api_key`. Used by callers so they don't
        have to know about the migration.
        """
        return self.secrets.anthropic_api_key or self.anthropic_api_key

    def resolve_model(self) -> str:
        """Return the canonical default model."""
        return self.secrets.model or self.model

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

    def resolve_provider_env(self, model: str) -> dict[str, str]:
        """Return env-var overrides for non-Anthropic model providers.

        When the model is a third-party model served behind an
        Anthropic-compatible endpoint (e.g. Kimi K3 via Moonshot), we need to
        set ANTHROPIC_BASE_URL and swap the API key. Returns an empty dict for
        native Claude models.
        """
        return resolve_provider_env(model, self)


PROVIDER_ENDPOINTS: dict[str, str] = {
    "kimi": "https://api.moonshot.ai/anthropic",
}


def resolve_provider_env(model: str, cfg: "FleetConfig | None" = None) -> dict[str, str]:
    """Return env-var overrides for third-party model providers.

    Callable without a config (returns only the base URL) or with one (also
    resolves the API key from secrets).
    """
    if model.startswith("kimi-"):
        env: dict[str, str] = {"ANTHROPIC_BASE_URL": PROVIDER_ENDPOINTS["kimi"]}
        if cfg:
            key = cfg.secrets.kimi_api_key
            if key:
                env["ANTHROPIC_API_KEY"] = key
                env["CLAUDE_CODE_API_KEY"] = key
        return env
    return {}


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
    ssh_host: str = ""  # explicit SSH target (overrides ip when set; empty = not SSH-reachable)
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
    cwd: str = ""  # working directory on the worker machine (for cfleet attach)
    github_level: str = "none"  # none | read | triage | write
    local_mode: bool = False  # True when started via `cfleet agent`
    skip_permissions: bool = True  # SDK runs in bypassPermissions; attach passes --dangerously-skip-permissions
    agent_backend: str = "claude"  # claude | prime — which agent runtime the worker drives
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
