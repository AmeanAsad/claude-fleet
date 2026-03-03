"""Ansible invocation wrapper for bootstrapping fleet workers."""

from __future__ import annotations

import tempfile
from pathlib import Path

import ansible_runner


class ProvisionError(Exception):
    pass


# Path to the ansible directory shipped with open-fleet
ANSIBLE_DIR = Path(__file__).parent.parent.parent / "ansible"


def bootstrap_worker(
    ip: str,
    worker_name: str,
    model: str,
    repos: list[dict],
    fleet_config,  # FleetConfig — avoid circular import
) -> None:
    """Run the bootstrap playbook against a single worker IP.

    Uses ansible-runner to invoke the playbook programmatically.
    """
    ssh_key = str(fleet_config.resolve_ssh_key())
    ssh_user = fleet_config.cloud.ssh_user

    extravars = {
        "target_host": "all",
        "ansible_user": ssh_user,
        "ansible_ssh_private_key_file": ssh_key,
        "ansible_ssh_common_args": "-o StrictHostKeyChecking=no",
        "worker_name": worker_name,
        "model": model,
        "repos": repos,
        "workspace_dir": "/workspace",
        "anthropic_api_key": fleet_config.anthropic_api_key,
    }

    # Optional paths — only pass if they exist
    skills_dir = fleet_config.resolve_skills_dir()
    if skills_dir.exists():
        extravars["skills_dir"] = str(skills_dir)

    claude_md = fleet_config.resolve_claude_md()
    if claude_md.exists():
        extravars["claude_md_path"] = str(claude_md)

    mcp_config = fleet_config.resolve_mcp_config()
    if mcp_config.exists():
        extravars["mcp_config_path"] = str(mcp_config)

    secrets_env = fleet_config.resolve_secrets_env()
    if secrets_env.exists():
        extravars["secrets_env_path"] = str(secrets_env)

    # Write a temp inventory file — passing "ip," inline causes ansible_runner
    # to include the trailing comma in the hostname, breaking SSH resolution.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False) as inv:
        inv.write(f"{ip}\n")
        inv_path = inv.name

    r = ansible_runner.run(
        private_data_dir=str(ANSIBLE_DIR),
        playbook="playbooks/bootstrap.yml",
        inventory=inv_path,
        extravars=extravars,
        envvars={"ANSIBLE_ROLES_PATH": str(ANSIBLE_DIR / "roles")},
    )

    Path(inv_path).unlink(missing_ok=True)

    if r.rc != 0:
        stdout_text = r.stdout.read() if hasattr(r.stdout, "read") else str(r.stdout)
        raise ProvisionError(f"Ansible bootstrap failed for {worker_name}:\n{stdout_text}")
