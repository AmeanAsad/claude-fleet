"""Typer CLI — all cfleet commands."""

from __future__ import annotations

import os
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="cfleet",
    help="Orchestrate long-running Claude Code instances on cloud VMs or local containers.",
    no_args_is_help=True,
)
machine_app = typer.Typer(help="Manage machines (VMs / container hosts).")
app.add_typer(machine_app, name="machine")



console = Console()


def _engine():
    """Lazy-load engine to avoid import overhead on --help."""
    from cfleet.engine import FleetEngine
    return FleetEngine()


def _detect_default_provider() -> str:
    import shutil as _shutil
    if _shutil.which("az"):
        return "azure"
    if _shutil.which("gcloud"):
        return "gcp"
    return "devcontainer"


def _validate_enum(value: str, enum_cls, flag_name: str):
    try:
        return enum_cls(value)
    except ValueError:
        choices = ", ".join(e.value for e in enum_cls)
        console.print(f"[red]Invalid {flag_name} '{value}'. Choose: {choices}[/red]")
        raise typer.Exit(1)


    """Set a worker's GitHub level in state and print branch protection warnings."""
    from cfleet.config import FleetConfig, FleetState

    state = FleetState.load()
    if worker_name not in state.workers:
        return
    old = state.workers[worker_name].github_level
    state.workers[worker_name].github_level = level
    state.save()
    console.print(f"GitHub access: [bold]{old}[/bold] → [bold]{level}[/bold]")

    if level == "write":
        _warn_branch_protection(state.workers[worker_name].repos)

    if level == "none" and old != "none":
        console.print("[dim]GitHub access revoked. Cached tokens expire within 1 hour.[/dim]")

