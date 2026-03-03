#!/usr/bin/env python3
"""Dynamic Ansible inventory script that reads ~/.cfleet/state.json.

Generates Ansible-compatible JSON inventory grouped by worker status.
Usage:
    ansible-playbook -i inventory/fleet.py -l worker-name playbooks/bootstrap.yml
"""

import json
import sys
from pathlib import Path

STATE_PATH = Path.home() / ".cfleet" / "state.json"


def main():
    inventory = {
        "_meta": {"hostvars": {}},
        "all": {"hosts": [], "children": ["fleet_workers"]},
        "fleet_workers": {"hosts": []},
    }

    if not STATE_PATH.exists():
        json.dump(inventory, sys.stdout, indent=2)
        return

    state = json.loads(STATE_PATH.read_text())
    workers = state.get("workers", {})

    # Group workers by status
    status_groups: dict[str, list[str]] = {}

    for name, worker in workers.items():
        ip = worker.get("ip", "")
        if not ip:
            continue

        inventory["fleet_workers"]["hosts"].append(name)
        inventory["_meta"]["hostvars"][name] = {
            "ansible_host": ip,
            "worker_status": worker.get("status", "unknown"),
            "worker_model": worker.get("model", ""),
            "worker_repos": worker.get("repos", []),
        }

        status = worker.get("status", "unknown")
        status_groups.setdefault(status, []).append(name)

    for status, hosts in status_groups.items():
        group_name = f"fleet_{status}"
        inventory[group_name] = {"hosts": hosts}

    json.dump(inventory, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
