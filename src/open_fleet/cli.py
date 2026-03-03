"""Typer CLI — all fleet commands."""

from __future__ import annotations

from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="fleet",
    help="Orchestrate long-running Claude Code instances on cloud VMs.",
    no_args_is_help=True,
)
console = Console()


def _engine():
    """Lazy-load engine to avoid import overhead on --help."""
    from open_fleet.engine import FleetEngine
    return FleetEngine()


# --------------------------------------------------------------------------
# fleet init
# --------------------------------------------------------------------------

@app.command()
def init(
    config_file: Optional[str] = typer.Option(
        None, "--config", "-c",
        help="Path to local fleet.yml init config (defaults to ./fleet.yml)",
    ),
):
    """Create ~/.fleet/ directory with example config, CLAUDE.md, skills/. Initialize Pulumi stack."""
    import yaml as _yaml
    from pathlib import Path
    from open_fleet.config import FleetConfig, FLEET_DIR, CONFIG_PATH
    from open_fleet.engine import FleetEngine

    # init can run before config exists, so handle that
    try:
        config = FleetConfig.load()
    except FileNotFoundError:
        config = FleetConfig()

    # Read local fleet.yml and merge values into config
    init_file = Path(config_file) if config_file else Path("fleet.yml")
    if init_file.exists():
        console.print(f"Reading init config from [bold]{init_file}[/bold]")
        overrides = _yaml.safe_load(init_file.read_text()) or {}

        if overrides.get("anthropic_api_key"):
            config.anthropic_api_key = overrides["anthropic_api_key"]

        cloud = overrides.get("cloud", {})
        azure = cloud.get("azure", {})
        if azure.get("subscription_id"):
            config.cloud.azure.subscription_id = azure["subscription_id"]

    # Fall back to interactive prompts for anything still missing
    if not config.anthropic_api_key:
        api_key = typer.prompt("Anthropic API key", hide_input=True)
        config.anthropic_api_key = api_key

    if not config.cloud.azure.subscription_id:
        console.print(
            "[dim]Tip: run [bold]az account show --query id -o tsv[/bold] to get your subscription ID[/dim]"
        )
        sub_id = typer.prompt("Azure subscription ID")
        config.cloud.azure.subscription_id = sub_id

    engine = FleetEngine(config=config)
    engine.init()


# --------------------------------------------------------------------------
# fleet spawn
# --------------------------------------------------------------------------

@app.command()
def spawn(
    name: str = typer.Argument(..., help="Worker name"),
    repo: list[str] = typer.Option([], "--repo", "-r", help="Repos to clone (repeatable, defaults to all)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override default model"),
    vm_type: Optional[str] = typer.Option(None, "--vm-type", help="VM type: regular, snp, or tdx"),
    instance_type: Optional[str] = typer.Option(None, "--instance-type", "-t", help="Override exact Azure SKU"),
    region: Optional[str] = typer.Option(None, "--region", help="Override default region"),
):
    """Spawn a new fleet worker VM."""
    from open_fleet.config import VMType

    resolved_vm_type = None
    if vm_type:
        try:
            resolved_vm_type = VMType(vm_type)
        except ValueError:
            console.print(f"[red]Invalid --vm-type '{vm_type}'. Choose: regular, snp, tdx[/red]")
            raise typer.Exit(1)

    engine = _engine()
    engine.spawn(
        name=name,
        repos=repo or None,
        model=model,
        vm_type=resolved_vm_type,
        instance_type=instance_type,
        region=region,
    )


# --------------------------------------------------------------------------
# fleet ls
# --------------------------------------------------------------------------

@app.command(name="ls")
def list_workers():
    """List all fleet workers."""
    engine = _engine()
    workers = engine.list_workers()

    if not workers:
        console.print("No workers. Run [bold]fleet spawn <name>[/bold] to create one.")
        return

    # Live-check workers that claim to be "working" — update to idle if Claude
    # Code is actually at its prompt.
    from open_fleet.ssh import WorkerSSH

    state_dirty = False
    for w in workers:
        if w.status == "working" and w.ip:
            try:
                ssh = WorkerSSH(
                    ip=w.ip,
                    user=engine.config.cloud.ssh_user,
                    key_path=str(engine.config.resolve_ssh_key()),
                )
                if ssh.is_claude_idle():
                    w.status = "idle"
                    state_dirty = True
                ssh.close()
            except Exception:
                pass
    if state_dirty:
        engine.state.save()

    table = Table(title="Fleet Workers")
    table.add_column("Name", style="bold")
    table.add_column("Status")
    table.add_column("VM Type")
    table.add_column("SKU")
    table.add_column("IP")
    table.add_column("Model")
    table.add_column("Last Prompt")

    status_colors = {
        "idle": "green",
        "working": "yellow",
        "spawning": "cyan",
        "provisioning": "cyan",
        "errored": "red",
        "stopped": "dim",
    }

    vm_type_colors = {
        "regular": "white",
        "snp": "magenta",
        "tdx": "blue",
    }

    for w in workers:
        color = status_colors.get(w.status, "white")
        vt_color = vm_type_colors.get(w.vm_type, "white")
        prompt_display = w.last_prompt[:50] + "..." if w.last_prompt and len(w.last_prompt) > 50 else (w.last_prompt or "—")
        table.add_row(
            w.name,
            f"[{color}]{w.status}[/{color}]",
            f"[{vt_color}]{w.vm_type}[/{vt_color}]",
            w.instance_type,
            w.ip or "—",
            w.model,
            prompt_display,
        )

    console.print(table)


# --------------------------------------------------------------------------
# fleet ask
# --------------------------------------------------------------------------

@app.command()
def ask(
    name: str = typer.Argument(..., help="Worker name"),
    prompt: str = typer.Argument(..., help="Prompt to send"),
):
    """Send a prompt to a worker. Fire and forget."""
    engine = _engine()
    engine.ask(name, prompt)


# --------------------------------------------------------------------------
# fleet attach
# --------------------------------------------------------------------------

@app.command()
def attach(
    name: str = typer.Argument(..., help="Worker name"),
):
    """Attach to a worker's tmux session (interactive). Ctrl+B d to detach."""
    engine = _engine()
    engine.attach(name)


# --------------------------------------------------------------------------
# fleet send
# --------------------------------------------------------------------------

@app.command()
def send(
    name: str = typer.Argument(..., help="Worker name"),
    local_path: str = typer.Argument(..., help="Local path to send"),
    to: str = typer.Option("/workspace/inbox/", "--to", help="Remote destination path"),
):
    """Send files to a worker via rsync."""
    engine = _engine()
    engine.send(name, local_path, to)


# --------------------------------------------------------------------------
# fleet collect
# --------------------------------------------------------------------------

@app.command()
def collect(
    name: str = typer.Argument(..., help="Worker name"),
    local_dest: str = typer.Argument(..., help="Local destination path"),
    path: str = typer.Option("/workspace/", "--path", help="Remote path to collect"),
):
    """Collect files from a worker via rsync."""
    engine = _engine()
    engine.collect(name, local_dest, path)


# --------------------------------------------------------------------------
# fleet logs
# --------------------------------------------------------------------------

@app.command()
def logs(
    name: str = typer.Argument(..., help="Worker name"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream logs continuously"),
    lines: int = typer.Option(100, "--lines", "-n", help="Number of lines to show"),
):
    """Show tmux output from a worker."""
    engine = _engine()
    engine.logs(name, lines=lines, follow=follow)


# --------------------------------------------------------------------------
# fleet status
# --------------------------------------------------------------------------

@app.command()
def status(
    name: str = typer.Argument(..., help="Worker name"),
):
    """Show detailed status for a worker."""
    engine = _engine()
    info = engine.status(name)

    console.print(f"\n[bold]{info['name']}[/bold]")
    console.print(f"  Status:     {info['status']}")
    console.print(f"  VM Type:    {info['vm_type']}")
    console.print(f"  SKU:        {info['instance_type']}")
    console.print(f"  IP:         {info['ip'] or '—'}")
    console.print(f"  Provider:   {info['provider']}")
    console.print(f"  Model:      {info['model']}")
    console.print(f"  Repos:      {', '.join(info['repos']) if info['repos'] else '—'}")
    console.print(f"  Tmux alive: {info.get('tmux_alive', '—')}")
    console.print(f"  Uptime:     {info.get('uptime', '—')}")
    console.print(f"  Created:    {info['created_at']}")
    if info['last_prompt']:
        console.print(f"  Last prompt: {info['last_prompt']}")
        console.print(f"  Prompt at:   {info['last_prompt_at']}")
    console.print()


# --------------------------------------------------------------------------
# fleet kill
# --------------------------------------------------------------------------

@app.command()
def kill(
    name: Optional[str] = typer.Argument(None, help="Worker name (omit with --all)"),
    all_workers: bool = typer.Option(False, "--all", help="Kill all workers"),
    collect_to: Optional[str] = typer.Option(None, "--collect", help="Collect files before killing"),
    force: bool = typer.Option(False, "--force", help="Force kill even if state is inconsistent"),
):
    """Destroy a worker VM (or all with --all)."""
    engine = _engine()

    if all_workers:
        engine.kill_all(collect_path=collect_to)
    elif name:
        engine.kill(name, collect_path=collect_to, force=force)
    else:
        console.print("[red]Provide a worker name or --all[/red]")
        raise typer.Exit(1)


# --------------------------------------------------------------------------
# fleet serve
# --------------------------------------------------------------------------

@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", "-H", help="Bind address"),
    port: int = typer.Option(8420, "--port", "-p", help="Port"),
):
    """Start the fleet web dashboard."""
    import uvicorn
    from open_fleet.api import create_app

    console.print(f"Fleet dashboard at [bold]http://{host}:{port}[/bold]")
    uvicorn.run(create_app(), host=host, port=port)


# --------------------------------------------------------------------------
# fleet tui
# --------------------------------------------------------------------------

@app.command()
def tui():
    """Launch the interactive TUI."""
    from open_fleet.tui import FleetTUI
    app_tui = FleetTUI()
    app_tui.run()
