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
console = Console()


def _engine():
    """Lazy-load engine to avoid import overhead on --help."""
    from cfleet.engine import FleetEngine
    return FleetEngine()


def _detect_default_provider() -> str:
    """Auto-detect the best default provider based on what's installed.

    Returns 'devcontainer' if no cloud CLI is found, otherwise the first
    available cloud CLI.
    """
    import shutil as _shutil
    if _shutil.which("az"):
        return "azure"
    if _shutil.which("gcloud"):
        return "gcp"
    return "devcontainer"


# --------------------------------------------------------------------------
# cfleetinit
# --------------------------------------------------------------------------

@app.command()
def init(
    config_file: Optional[str] = typer.Option(
        None, "--config", "-c",
        help="Path to local fleet.yml init config (defaults to ./fleet.yml)",
    ),
):
    """Create ~/.cfleet/ directory with example config, CLAUDE.md, skills/. Initialize stack."""
    import yaml as _yaml
    from pathlib import Path
    from cfleet.config import FleetConfig, FLEET_DIR, CONFIG_PATH, PROVIDER_DEFAULTS
    from cfleet.engine import FleetEngine

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
        if cloud.get("provider"):
            config.cloud.provider = cloud["provider"]
        if cloud.get("region"):
            config.cloud.region = cloud["region"]
        if cloud.get("ssh_user"):
            config.cloud.ssh_user = cloud["ssh_user"]
        if cloud.get("instance_type"):
            config.cloud.instance_type = cloud["instance_type"]
        azure = cloud.get("azure", {})
        if azure.get("subscription_id"):
            config.cloud.azure.subscription_id = azure["subscription_id"]
        gcp = cloud.get("gcp", {})
        if gcp.get("project_id"):
            config.cloud.gcp.project_id = gcp["project_id"]
        if gcp.get("zone"):
            config.cloud.gcp.zone = gcp["zone"]

    # --- Provider selection (must come first) ---
    if not config.cloud.provider:
        # Auto-detect: if no cloud CLI is installed, default to devcontainer
        default_provider = _detect_default_provider()
        provider = typer.prompt(
            "Provider",
            default=default_provider,
            show_choices=True,
            type=typer.Choice(["devcontainer", "azure", "gcp"]),
        )
        config.cloud.provider = provider

    # Apply provider defaults for any fields not explicitly set
    defaults = PROVIDER_DEFAULTS.get(config.cloud.provider, {})
    if not config.cloud.region:
        config.cloud.region = defaults.get("region", "")
    if not config.cloud.ssh_user:
        config.cloud.ssh_user = defaults.get("ssh_user", "")
    if not config.cloud.instance_type:
        config.cloud.instance_type = defaults.get("instance_type", "")

    # --- API key ---
    if not config.anthropic_api_key:
        api_key = typer.prompt("Anthropic API key", hide_input=True)
        config.anthropic_api_key = api_key

    # --- Provider-specific fields ---
    provider = config.cloud.provider

    if provider == "azure":
        if not config.cloud.azure.subscription_id:
            console.print(
                "[dim]Tip: run [bold]az account show --query id -o tsv[/bold] to get your subscription ID[/dim]"
            )
            sub_id = typer.prompt("Azure subscription ID")
            config.cloud.azure.subscription_id = sub_id

        if not config.cloud.azure.resource_group:
            import secrets as _secrets
            slug = _secrets.token_hex(3)
            config.cloud.azure.resource_group = f"{slug}-fleet-workers"
            console.print(f"Resource group: [bold]{config.cloud.azure.resource_group}[/bold]")

    elif provider == "gcp":
        if not config.cloud.gcp.project_id:
            console.print(
                "[dim]Tip: run [bold]gcloud config get-value project[/bold] to get your project ID[/dim]"
            )
            project_id = typer.prompt("GCP project ID")
            config.cloud.gcp.project_id = project_id

    elif provider == "devcontainer":
        console.print("[dim]Using local Docker containers — no cloud credentials needed.[/dim]")

    if provider == "devcontainer":
        console.print(f"Provider: [bold]{provider}[/bold]  (local Docker)")
    else:
        console.print(f"Provider: [bold]{provider}[/bold]  Region: [bold]{config.cloud.region}[/bold]  "
                      f"SSH user: [bold]{config.cloud.ssh_user}[/bold]")

    engine = FleetEngine(config=config)
    engine.init()


# --------------------------------------------------------------------------
# cfleetspawn
# --------------------------------------------------------------------------

@app.command()
def spawn(
    name: str = typer.Argument(..., help="Worker name"),
    repo: list[str] = typer.Option([], "--repo", "-r", help="Repos to clone (repeatable, defaults to all)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override default model"),
    vm_type: Optional[str] = typer.Option(None, "--type", help="VM type: regular, snp, or tdx"),
    instance_type: Optional[str] = typer.Option(None, "--instance-type", "-t", help="Override machine type/SKU"),
    region: Optional[str] = typer.Option(None, "--region", help="Override default region"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Provider: devcontainer, azure, or gcp (defaults to config)"),
):
    """Spawn a new fleet worker."""
    from cfleet.config import VMType

    if provider and provider not in ("azure", "gcp", "devcontainer"):
        console.print(f"[red]Invalid --provider '{provider}'. Choose: devcontainer, azure, gcp[/red]")
        raise typer.Exit(1)

    resolved_vm_type = None
    if vm_type:
        try:
            resolved_vm_type = VMType(vm_type)
        except ValueError:
            console.print(f"[red]Invalid --type '{vm_type}'. Choose: regular, snp, tdx[/red]")
            raise typer.Exit(1)

    engine = _engine()
    engine.spawn(
        name=name,
        repos=repo or None,
        model=model,
        vm_type=resolved_vm_type,
        instance_type=instance_type,
        region=region,
        provider=provider,
    )


# --------------------------------------------------------------------------
# cfleetls
# --------------------------------------------------------------------------

@app.command(name="ls")
def list_workers():
    """List all fleet workers."""
    engine = _engine()
    workers = engine.list_workers()

    if not workers:
        console.print("No workers. Run [bold]cfleet spawn <name>[/bold] to create one.")
        return

    # Live-check workers that claim to be "working" — update to idle
    state_dirty = False
    for w in workers:
        reachable = w.status == "working" and (w.ip or w.container_id)
        if reachable:
            try:
                conn = engine._get_conn(w)
                if engine._uses_relay(w):
                    relay = conn.get_relay_client()
                    if relay.is_idle():
                        w.status = "idle"
                        state_dirty = True
                else:
                    if conn.is_claude_idle():
                        w.status = "idle"
                        state_dirty = True
                conn.close()
            except Exception as e:
                console.print(f"[dim]Could not check {w.name}: {e}[/dim]")
    if state_dirty:
        engine.state.save()

    table = Table(title="Fleet Workers")
    table.add_column("Name", style="bold")
    table.add_column("Status")
    table.add_column("Mode")
    table.add_column("Provider")
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

    mode_colors = {
        "relay": "green",
        "tmux": "yellow",
    }

    for w in workers:
        color = status_colors.get(w.status, "white")
        m_color = mode_colors.get(w.communication_mode, "white")
        prompt_display = w.last_prompt[:50] + "..." if w.last_prompt and len(w.last_prompt) > 50 else (w.last_prompt or "—")
        target = w.ip or (w.container_id[:12] if w.container_id else "—")
        table.add_row(
            w.name,
            f"[{color}]{w.status}[/{color}]",
            f"[{m_color}]{w.communication_mode}[/{m_color}]",
            w.provider,
            target,
            w.model,
            prompt_display,
        )

    console.print(table)


# --------------------------------------------------------------------------
# cfleetask
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
# cfleetinterrupt
# --------------------------------------------------------------------------

@app.command()
def interrupt(
    name: str = typer.Argument(..., help="Worker name"),
):
    """Interrupt the current agent run on a worker."""
    engine = _engine()
    engine.interrupt(name)


# --------------------------------------------------------------------------
# cfleetattach
# --------------------------------------------------------------------------

@app.command()
def attach(
    name: str = typer.Argument(..., help="Worker name"),
):
    """Attach to a worker's shell for debugging. Replaces current process."""
    engine = _engine()
    engine.attach(name)


# --------------------------------------------------------------------------
# cfleetsend
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
# cfleetcollect
# --------------------------------------------------------------------------

@app.command()
def collect(
    name: str = typer.Argument(..., help="Worker name"),
    local_dest: str = typer.Argument(..., help="Local destination path"),
    path: str = typer.Option("/workspace/outbox/", "--path", help="Remote path to collect"),
):
    """Collect files from a worker via rsync."""
    engine = _engine()
    engine.collect(name, local_dest, path)


# --------------------------------------------------------------------------
# cfleetlogs
# --------------------------------------------------------------------------

@app.command()
def logs(
    name: str = typer.Argument(..., help="Worker name"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream logs continuously"),
    lines: int = typer.Option(100, "--lines", "-n", help="Number of messages to show"),
):
    """Show structured conversation logs from a worker."""
    engine = _engine()
    engine.logs(name, lines=lines, follow=follow)


# --------------------------------------------------------------------------
# cfleetstatus
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
    console.print(f"  Mode:       {info.get('communication_mode', 'tmux')}")
    console.print(f"  Provider:   {info['provider']}")
    if info['provider'] == "devcontainer":
        console.print(f"  Container:  {info.get('container_id', '—')[:12] or '—'}")
    else:
        console.print(f"  IP:         {info['ip'] or '—'}")
    console.print(f"  Model:      {info['model']}")
    console.print(f"  Repos:      {', '.join(info['repos']) if info['repos'] else '—'}")

    # Relay-specific info
    if info.get('communication_mode') == 'relay':
        console.print(f"  Relay:      {'[green]alive[/green]' if info.get('relay_alive') else '[red]dead[/red]'}")
        if info.get('session_id'):
            console.print(f"  Session:    {info['session_id']}")
        inp = info.get('total_input_tokens', 0)
        out = info.get('total_output_tokens', 0)
        cost = info.get('total_cost_usd', 0.0)
        if inp or out:
            console.print(f"  Tokens:     {inp:,} in / {out:,} out")
            console.print(f"  Cost:       ${cost:.4f}")
        msg_count = info.get('message_count', 0)
        if msg_count:
            console.print(f"  Messages:   {msg_count}")
    else:
        console.print(f"  Tmux alive: {info.get('tmux_alive', '—')}")

    console.print(f"  Uptime:     {info.get('uptime', '—')}")
    console.print(f"  Created:    {info['created_at']}")
    if info.get('last_prompt'):
        console.print(f"  Last prompt: {info['last_prompt']}")
        console.print(f"  Prompt at:   {info['last_prompt_at']}")
    console.print()


# --------------------------------------------------------------------------
# cfleetkill
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
# cfleetserve
# --------------------------------------------------------------------------

@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", "-H", help="Bind address"),
    port: int = typer.Option(8420, "--port", "-p", help="Port"),
):
    """Start the fleet web dashboard."""
    import uvicorn
    from cfleet.api import create_app

    from cfleet.config import FleetConfig
    try:
        cfg = FleetConfig.load()
        token = cfg.api.token
    except FileNotFoundError:
        token = ""
    if not token and not os.environ.get("FLEET_API_TOKEN"):
        console.print(
            "[yellow]Warning: No API token set. The dashboard is open to anyone who can reach it.\n"
            "Set 'api.token' in ~/.cfleet/config.yml or export FLEET_API_TOKEN.[/yellow]"
        )
    console.print(f"Claude Fleet dashboard at [bold]http://{host}:{port}[/bold]")
    uvicorn.run(create_app(), host=host, port=port)


# --------------------------------------------------------------------------
# cfleettui
# --------------------------------------------------------------------------

@app.command()
def tui():
    """Launch the interactive TUI."""
    from cfleet.tui import FleetTUI
    app_tui = FleetTUI()
    app_tui.run()
