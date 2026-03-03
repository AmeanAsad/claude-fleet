"""Pydantic models for fleet config (~/.fleet/config.yml) and state (~/.fleet/state.json)."""

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


# Default Azure SKUs per VM type
DEFAULT_SKUS: dict[VMType, str] = {
    VMType.REGULAR: "Standard_D2s_v5",
    VMType.SNP: "Standard_DC4as_v5",
    VMType.TDX: "Standard_DC4es_v6",
}


FLEET_DIR = Path.home() / ".fleet"
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
    resource_group: str = "fleet-workers"
    image: AzureImageConfig = AzureImageConfig()
    vnet: Optional[str] = None
    subnet: Optional[str] = None


class CloudConfig(BaseModel):
    provider: str = "azure"
    region: str = "westeurope"
    vm_type: VMType = VMType.REGULAR
    instance_type: str = "Standard_D2s_v5"
    ssh_key: str = "~/.ssh/id_ed25519"
    ssh_user: str = "azureuser"
    azure: AzureConfig = AzureConfig()


class ApiConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8420
    token: str = ""  # empty = no auth


class PulumiConfig(BaseModel):
    project: str = "open-fleet"
    stack: str = "default"
    backend: str = "file://~/.fleet/pulumi-state"


class FleetConfig(BaseModel):
    anthropic_api_key: str = ""
    model: str = "claude-opus-4-6"
    secrets_env: str = "~/.fleet/secrets.env"
    repos: list[RepoConfig] = Field(default_factory=list)
    skills_dir: str = "~/.fleet/skills/"
    claude_md: str = "~/.fleet/CLAUDE.md"
    mcp_config: str = "~/.fleet/mcp-servers.json"
    pulumi: PulumiConfig = PulumiConfig()
    cloud: CloudConfig = CloudConfig()
    api: ApiConfig = ApiConfig()

    @classmethod
    def load(cls, path: Path | None = None) -> FleetConfig:
        p = path or CONFIG_PATH
        if not p.exists():
            raise FileNotFoundError(f"Config not found at {p}. Run 'fleet init' first.")
        raw = yaml.safe_load(p.read_text()) or {}
        return cls.model_validate(raw)

    def save(self, path: Path | None = None) -> None:
        p = path or CONFIG_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.dump(self.model_dump(mode="json"), default_flow_style=False, sort_keys=False))

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


class WorkerState(BaseModel):
    name: str
    status: str = "spawning"  # spawning | provisioning | idle | working | errored | stopped
    ip: str = ""
    provider: str = "azure"
    vm_type: str = "regular"  # regular | snp | tdx
    instance_type: str = ""
    model: str = ""
    repos: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_prompt: Optional[str] = None
    last_prompt_at: Optional[str] = None


class FleetState(BaseModel):
    workers: dict[str, WorkerState] = Field(default_factory=dict)

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

    def get_worker(self, name: str) -> WorkerState:
        if name not in self.workers:
            raise KeyError(f"Worker '{name}' not found. Run 'fleet ls' to see workers.")
        return self.workers[name]

    def add_worker(self, worker: WorkerState) -> None:
        self.workers[worker.name] = worker

    def remove_worker(self, name: str) -> None:
        self.workers.pop(name, None)
