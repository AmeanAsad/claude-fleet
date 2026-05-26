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

gh_app = typer.Typer(help="GitHub App token broker — manage per-worker GitHub access.")
app.add_typer(gh_app, name="gh")

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


def _set_worker_gh_level(worker_name: str, level: str) -> None:
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


def _warn_branch_protection(repos: list[str]) -> None:
    """Print warnings for repos missing branch protection. Best-effort, never raises."""
    from cfleet.config import FleetConfig
    try:
        config = FleetConfig.load()
    except FileNotFoundError:
        return
    if not config.github.is_configured():
        return

    from cfleet.github import warn_unprotected_repos
    for warning in warn_unprotected_repos(config, repos):
        console.print(f"[yellow]Warning: {warning}[/yellow]")


# --------------------------------------------------------------------------
# cfleet init
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

    try:
        config = FleetConfig.load()
    except FileNotFoundError:
        config = FleetConfig()

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

    if not config.cloud.provider:
        default_provider = _detect_default_provider()
        provider = typer.prompt(
            "Provider",
            default=default_provider,
            show_choices=True,
            type=typer.Choice(["devcontainer", "azure", "gcp"]),
        )
        config.cloud.provider = provider

    defaults = PROVIDER_DEFAULTS.get(config.cloud.provider, {})
    if not config.cloud.region:
        config.cloud.region = defaults.get("region", "")
    if not config.cloud.ssh_user:
        config.cloud.ssh_user = defaults.get("ssh_user", "")
    if not config.cloud.instance_type:
        config.cloud.instance_type = defaults.get("instance_type", "")

    if not config.anthropic_api_key:
        api_key = typer.prompt("Anthropic API key", hide_input=True)
        config.anthropic_api_key = api_key

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
# cfleet machine create
# --------------------------------------------------------------------------

@machine_app.command("create")
def machine_create(
    name: str = typer.Argument(..., help="Machine name"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Provider: devcontainer, azure, or gcp"),
    vm_type: Optional[str] = typer.Option(None, "--type", help="VM type: regular, snp, or tdx"),
    instance_type: Optional[str] = typer.Option(None, "--instance-type", "-t", help="Override machine type/SKU"),
    region: Optional[str] = typer.Option(None, "--region", help="Override default region"),
):
    """Create a new machine (VM or container host)."""
    from cfleet.config import VMType

    resolved_vm_type = _validate_enum(vm_type, VMType, "--type") if vm_type else None

    engine = _engine()
    engine.create_machine(
        name=name,
        provider=provider,
        vm_type=resolved_vm_type,
        instance_type=instance_type,
        region=region,
    )


# --------------------------------------------------------------------------
# cfleet machine ls
# --------------------------------------------------------------------------

@machine_app.command("ls")
def machine_ls():
    """List all machines."""
    engine = _engine()
    machines = engine.list_machines()

    if not machines:
        console.print("No machines. Run [bold]cfleet machine create <name>[/bold] to create one.")
        return

    table = Table(title="Machines")
    table.add_column("Name", style="bold")
    table.add_column("Provider")
    table.add_column("IP")
    table.add_column("Region")
    table.add_column("Type")
    table.add_column("Workers")
    table.add_column("Status")

    status_colors = {
        "ready": "green",
        "creating": "cyan",
        "provisioning": "cyan",
        "errored": "red",
        "stopped": "dim",
        "disconnected": "yellow",
    }

    for m in machines:
        color = status_colors.get(m.status, "white")
        ip_display = m.ip or m.hostname or (m.container_id[:12] if m.container_id else "-")
        workers_display = ", ".join(m.worker_names) if m.worker_names else "-"
        table.add_row(
            m.name,
            m.provider,
            ip_display,
            m.region or "-",
            m.instance_type,
            workers_display,
            f"[{color}]{m.status}[/{color}]",
        )

    console.print(table)


# --------------------------------------------------------------------------
# cfleet machine rm
# --------------------------------------------------------------------------

@machine_app.command("rm")
def machine_rm(
    name: str = typer.Argument(..., help="Machine name"),
    keep_vm: bool = typer.Option(False, "--keep-vm", help="Remove from state but keep the cloud VM"),
    purge: bool = typer.Option(False, "--purge", help="Remove from state without attempting remote cleanup"),
):
    """Remove a machine and all its workers.

    If the machine was already destroyed externally or is unreachable,
    use --purge to clean up state without contacting the cloud provider.
    """
    engine = _engine()
    engine.remove_machine(name, keep_vm=keep_vm, purge=purge)


# --------------------------------------------------------------------------
# cfleet machine ssh
# --------------------------------------------------------------------------

@machine_app.command("ssh")
def machine_ssh(
    name: str = typer.Argument(..., help="Machine name"),
):
    """SSH into a machine. Replaces current process."""
    engine = _engine()
    machine = engine.state.get_machine(name)

    if machine.provider == "devcontainer":
        console.print("[yellow]Use 'cfleet attach <worker>' for devcontainer machines.[/yellow]")
        raise typer.Exit(1)

    from cfleet.ssh import ssh_attach
    ssh_user = machine.ssh_user or engine.config.resolve_ssh_user(provider=machine.provider)
    ssh_attach(machine.ip, ssh_user, str(engine.config.resolve_ssh_key()))


# --------------------------------------------------------------------------
# cfleet spawn
# --------------------------------------------------------------------------

@app.command()
def spawn(
    name: str = typer.Argument(..., help="Worker name"),
    machine: Optional[str] = typer.Option(None, "--machine", "-M", help="Machine to spawn on (auto-creates if omitted)"),
    repo: list[str] = typer.Option([], "--repo", "-r", help="Repos to clone (repeatable, defaults to all)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override default model"),
    vm_type: Optional[str] = typer.Option(None, "--type", help="VM type for auto-created machine: regular, snp, or tdx"),
    instance_type: Optional[str] = typer.Option(None, "--instance-type", "-t", help="Override machine type/SKU (auto-create only)"),
    region: Optional[str] = typer.Option(None, "--region", help="Override default region (auto-create only)"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Provider for auto-created machine"),
    gh: Optional[str] = typer.Option(None, "--gh", help="GitHub access level: read, triage, or write"),
):
    """Spawn a new fleet worker on a machine."""
    from cfleet.config import GitHubLevel, VMType

    if gh:
        _validate_enum(gh, GitHubLevel, "--gh")

    resolved_vm_type = None
    if vm_type:
        resolved_vm_type = _validate_enum(vm_type, VMType, "--type")

    engine = _engine()
    engine.spawn(
        name=name,
        machine_name=machine,
        repos=repo or None,
        model=model,
        vm_type=resolved_vm_type,
        instance_type=instance_type,
        region=region,
        provider=provider,
    )

    if gh:
        _set_worker_gh_level(name, gh)


# --------------------------------------------------------------------------
# cfleet ls
# --------------------------------------------------------------------------

@app.command(name="ls")
def list_workers():
    """List all fleet workers."""
    engine = _engine()
    workers = engine.list_workers()

    if not workers:
        console.print("No workers. Run [bold]cfleet spawn <name>[/bold] to create one.")
        return

    table = Table(title="Fleet Workers")
    table.add_column("Name", style="bold")
    table.add_column("Machine")
    table.add_column("Status")
    table.add_column("Port")
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

    for w in workers:
        color = status_colors.get(w.status, "white")
        prompt_display = w.last_prompt[:50] + "..." if w.last_prompt and len(w.last_prompt) > 50 else (w.last_prompt or "-")
        table.add_row(
            w.name,
            w.machine_name or "-",
            f"[{color}]{w.status}[/{color}]",
            str(w.relay_port),
            w.model,
            prompt_display,
        )

    console.print(table)


# --------------------------------------------------------------------------
# cfleet ask
# --------------------------------------------------------------------------

@app.command()
def ask(
    name: str = typer.Argument(..., help="Worker name"),
    prompt: str = typer.Argument(..., help="Prompt to send"),
):
    """Send a prompt to a worker."""
    engine = _engine()
    engine.ask(name, prompt)


# --------------------------------------------------------------------------
# cfleet interrupt
# --------------------------------------------------------------------------

@app.command()
def interrupt(
    name: str = typer.Argument(..., help="Worker name"),
):
    """Interrupt the current agent run on a worker."""
    engine = _engine()
    engine.interrupt(name)


# --------------------------------------------------------------------------
# cfleet attach
# --------------------------------------------------------------------------

@app.command()
def attach(
    name: str = typer.Argument(..., help="Worker name"),
):
    """Attach to a worker's machine shell for debugging. Replaces current process."""
    engine = _engine()
    engine.attach(name)


# --------------------------------------------------------------------------
# cfleet send
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
# cfleet collect
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
# cfleet logs
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
# cfleet status
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
    console.print(f"  Machine:    {info['machine_name']}")
    console.print(f"  Provider:   {info.get('provider', '-')}")
    console.print(f"  IP:         {info.get('machine_ip', '-')}")
    console.print(f"  Relay port: {info['relay_port']}")
    console.print(f"  Model:      {info['model']}")
    console.print(f"  Repos:      {', '.join(info['repos']) if info['repos'] else '-'}")

    relay_alive = info.get('relay_alive')
    if relay_alive is not None:
        console.print(f"  Relay:      {'[green]alive[/green]' if relay_alive else '[red]dead[/red]'}")
    if info.get('session_id'):
        console.print(f"  Session:    {info['session_id']}")
    msg_count = info.get('message_count', 0)
    if msg_count:
        console.print(f"  Messages:   {msg_count}")

    console.print(f"  Created:    {info['created_at']}")
    if info.get('last_prompt'):
        console.print(f"  Last prompt: {info['last_prompt']}")
        console.print(f"  Prompt at:   {info['last_prompt_at']}")
    console.print()


# --------------------------------------------------------------------------
# cfleet kill
# --------------------------------------------------------------------------

@app.command()
def kill(
    name: Optional[str] = typer.Argument(None, help="Worker name (omit with --all)"),
    all_workers: bool = typer.Option(False, "--all", help="Kill all workers and machines"),
    collect_to: Optional[str] = typer.Option(None, "--collect", help="Collect files before killing"),
    force: bool = typer.Option(False, "--force", help="Force kill even if state is inconsistent"),
    rm_machine: bool = typer.Option(False, "--remove-machine", help="Also remove the machine if empty after kill"),
    purge: bool = typer.Option(False, "--purge", help="Remove from state without attempting remote cleanup"),
):
    """Destroy a worker (or all with --all).

    If the worker or machine is unreachable, use --purge to remove it
    from state without attempting any remote cleanup.
    """
    engine = _engine()

    if all_workers:
        engine.kill_all(collect_path=collect_to)
    elif name:
        engine.kill(name, collect_path=collect_to, force=force, remove_machine=rm_machine, purge=purge)
    else:
        console.print("[red]Provide a worker name or --all[/red]")
        raise typer.Exit(1)


# --------------------------------------------------------------------------
# cfleet serve
# --------------------------------------------------------------------------

@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", "-H", help="Bind address"),
    port: int = typer.Option(8420, "--port", "-p", help="Port"),
    new_token: bool = typer.Option(False, "--new-token", help="Regenerate the server token"),
):
    """Start the central fleet server. Auto-generates a registration token on first run."""
    import uvicorn
    from cfleet.config import FleetConfig
    from cfleet.server import create_server_app, generate_server_token

    try:
        cfg = FleetConfig.load()
    except FileNotFoundError:
        console.print("[red]Run 'cfleet init' first.[/red]")
        raise typer.Exit(1)

    if new_token or (not cfg.server.token and not os.environ.get("FLEET_API_TOKEN")):
        token = generate_server_token()
        console.print(f"\n[bold green]Server token:[/bold green] {token}")
        console.print("[dim]Workers use this token to register. Pass it via CFLEET_TOKEN env var or --token flag.[/dim]\n")
    else:
        token = cfg.server.token or os.environ.get("FLEET_API_TOKEN", "")
        if token:
            console.print(f"[dim]Using existing token: {token[:8]}...[/dim]")

    console.print(f"Claude Fleet server at [bold]http://{host}:{port}[/bold]")
    console.print(f"Workers connect to [bold]ws://{host}:{port}/ws[/bold]")
    uvicorn.run(create_server_app(), host=host, port=port)


# --------------------------------------------------------------------------
# cfleet connect
# --------------------------------------------------------------------------

@app.command()
def connect(
    server_url: str = typer.Argument(..., help="Server URL (e.g. http://my-server:8420)"),
    token: Optional[str] = typer.Option(None, "--token", "-t", help="Server token for authentication"),
):
    """Connect this CLI to a remote fleet server."""
    from cfleet.config import FleetConfig

    try:
        cfg = FleetConfig.load()
    except FileNotFoundError:
        console.print("[red]Run 'cfleet init' first.[/red]")
        raise typer.Exit(1)

    cfg.server.url = server_url.rstrip("/")
    if token:
        cfg.server.token = token
    cfg.save()

    console.print(f"[green]Connected to server at {server_url}[/green]")
    if not token and not cfg.server.token:
        console.print("[yellow]No token set — set one with --token or in config.yml[/yellow]")


# --------------------------------------------------------------------------
# cfleet disconnect
# --------------------------------------------------------------------------

@app.command()
def disconnect():
    """Disconnect from the remote fleet server (use local SSH mode)."""
    from cfleet.config import FleetConfig

    try:
        cfg = FleetConfig.load()
    except FileNotFoundError:
        console.print("[red]Run 'cfleet init' first.[/red]")
        raise typer.Exit(1)

    cfg.server.url = ""
    cfg.save()
    console.print("[green]Disconnected from server. Using local SSH mode.[/green]")


# --------------------------------------------------------------------------
# cfleet tui
# --------------------------------------------------------------------------

@app.command()
def tui():
    """Launch the interactive TUI."""
    from cfleet.tui import FleetTUI
    app_tui = FleetTUI()
    app_tui.run()


# --------------------------------------------------------------------------
# cfleet join
# --------------------------------------------------------------------------

@app.command()
def join(
    server_url: str = typer.Argument(..., help="Server URL (e.g. http://my-server:8420)"),
    token: Optional[str] = typer.Option(None, "--token", "-t", help="Server token"),
    api_key: Optional[str] = typer.Option(None, "--api-key", help="Anthropic API key"),
    model: str = typer.Option("claude-opus-4-6", "--model", "-m", help="Default model"),
    skip_bootstrap: bool = typer.Option(False, "--skip-bootstrap", help="Skip system deps install"),
):
    """Join the fleet — save server config and optionally bootstrap this machine.

    Saves the server URL, token, and API key to ~/.cfleet/config.yml.
    Use `cfleet agent <name>` afterwards to start a worker.
    """
    from cfleet.config import FleetConfig, FLEET_DIR

    try:
        cfg = FleetConfig.load()
    except FileNotFoundError:
        FLEET_DIR.mkdir(parents=True, exist_ok=True)
        cfg = FleetConfig()

    effective_api_key = api_key or cfg.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    if not effective_api_key:
        effective_api_key = typer.prompt("Anthropic API key", hide_input=True)

    if not skip_bootstrap:
        from cfleet.provisioner import local_bootstrap
        local_bootstrap(api_key=effective_api_key, model=model)

    cfg.server.url = server_url.rstrip("/")
    if token:
        cfg.server.token = token
    if effective_api_key:
        cfg.anthropic_api_key = effective_api_key
    cfg.model = model
    cfg.save()

    console.print(f"[green]Joined fleet at {server_url}[/green]")
    console.print("[dim]Run 'cfleet agent <name>' to start a worker.[/dim]")


# --------------------------------------------------------------------------
# cfleet agent
# --------------------------------------------------------------------------

@app.command()
def agent(
    name: str = typer.Argument(..., help="Worker name"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override model"),
    cwd: Optional[str] = typer.Option(None, "--cwd", help="Working directory"),
    server_url: Optional[str] = typer.Option(None, "--server-url", "-s", help="Server URL (reads from config if omitted)"),
    token: Optional[str] = typer.Option(None, "--token", "-t", help="Server token"),
):
    """Start a Claude Code TUI session registered with the fleet.

    Wraps the `claude` CLI — launches the real TUI with your terminal attached.
    A background WebSocket connection registers this session with the server
    so the dashboard can view messages and send prompts.
    """
    import json
    import platform
    import subprocess
    import threading
    import time
    import uuid
    from pathlib import Path
    from cfleet.config import FleetConfig

    try:
        cfg = FleetConfig.load()
    except FileNotFoundError:
        console.print("[red]Run 'cfleet init' or 'cfleet join' first.[/red]")
        raise typer.Exit(1)

    effective_model = model or cfg.model
    effective_server_url = server_url or cfg.server.url
    effective_token = token or cfg.server.token
    machine_name = platform.node()
    workspace = cwd or os.getcwd()

    if not effective_server_url:
        console.print("[red]No server URL. Pass --server-url or run 'cfleet join' first.[/red]")
        raise typer.Exit(1)

    if not effective_token:
        console.print("[red]No server token. Pass --token or set it in config.yml[/red]")
        raise typer.Exit(1)

    api_key = cfg.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", api_key)

    session_id = str(uuid.uuid4())
    claude_cmd = ["claude", "--session-id", session_id, "--model", effective_model, "--name", name]

    # Derive session JSONL path: ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl
    encoded_cwd = workspace.replace("/", "-")
    jsonl_path = str(Path.home() / ".claude" / "projects" / encoded_cwd / f"{session_id}.jsonl")

    console.print(f"Starting worker [bold]{name}[/bold] on {machine_name}")
    console.print(f"  Server:  {effective_server_url}")
    console.print(f"  CWD:     {workspace}")
    console.print(f"  Model:   {effective_model}")
    console.print(f"  Session: {session_id}")

    # Background WebSocket client for server registration
    stop_event = threading.Event()

    def _ws_background():
        import asyncio
        asyncio.run(_agent_ws_loop(
            server_url=effective_server_url,
            token=effective_token,
            worker_name=name,
            machine_name=machine_name,
            model=effective_model,
            session_id=session_id,
            jsonl_path=jsonl_path,
            stop_event=stop_event,
        ))

    ws_thread = threading.Thread(target=_ws_background, daemon=True)
    ws_thread.start()

    try:
        proc = subprocess.run(claude_cmd, cwd=workspace)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        ws_thread.join(timeout=5)
        console.print(f"\n[dim]Worker {name} stopped.[/dim]")


async def _agent_ws_loop(
    server_url: str,
    token: str,
    worker_name: str,
    machine_name: str,
    model: str,
    session_id: str,
    jsonl_path: str,
    stop_event,
):
    """Background WebSocket client that registers with the server and handles commands."""
    import asyncio
    import json
    import websockets
    from pathlib import Path

    ws_url = server_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    backoff = 1.0

    while not stop_event.is_set():
        try:
            async with websockets.connect(ws_url) as ws:
                await ws.send(json.dumps({
                    "type": "register",
                    "token": token,
                    "worker_name": worker_name,
                    "machine_name": machine_name,
                }))

                reg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if reg.get("type") != "registered":
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue

                backoff = 1.0

                heartbeat_task = asyncio.create_task(_agent_heartbeat(ws, stop_event))

                try:
                    while not stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue
                        data = json.loads(raw)
                        await _handle_server_command(ws, data, jsonl_path, worker_name, model, session_id)
                finally:
                    heartbeat_task.cancel()

        except asyncio.CancelledError:
            break
        except Exception:
            if stop_event.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def _agent_heartbeat(ws, stop_event):
    import json
    while not stop_event.is_set():
        await asyncio.sleep(30)
        try:
            await ws.send(json.dumps({"type": "heartbeat"}))
        except Exception:
            break


async def _handle_server_command(ws, data: dict, jsonl_path: str, worker_name: str, model: str, session_id: str):
    """Handle commands from the server (ask, messages, status, interrupt)."""
    import json
    from pathlib import Path

    msg_type = data.get("type")
    request_id = data.get("request_id", "")

    if msg_type == "heartbeat_ack":
        return

    if msg_type == "ask":
        prompt = data.get("prompt", "")
        # Write queue-operation to the session JSONL so the running claude TUI picks it up
        from datetime import datetime, timezone
        queue_line = json.dumps({
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "sessionId": session_id,
            "content": prompt,
        })
        p = Path(jsonl_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(queue_line + "\n")

        await ws.send(json.dumps({
            "type": "response",
            "request_id": request_id,
            "data": {"ok": True},
        }))

    elif msg_type == "messages":
        offset = data.get("offset", 0)
        limit = data.get("limit", 200)
        messages = _read_session_messages(jsonl_path, offset, limit)
        await ws.send(json.dumps({
            "type": "response",
            "request_id": request_id,
            "data": {"messages": messages},
        }))

    elif msg_type == "status":
        await ws.send(json.dumps({
            "type": "response",
            "request_id": request_id,
            "data": {
                "worker_name": worker_name,
                "model": model,
                "session_id": session_id,
                "message_count": len(_read_session_messages(jsonl_path, 0, 10000)),
            },
        }))

    elif msg_type == "interrupt":
        # Find the claude process and send SIGINT
        import signal
        try:
            session_dir = Path.home() / ".claude" / "sessions"
            for sf in session_dir.iterdir():
                try:
                    meta = json.loads(sf.read_text())
                    if meta.get("sessionId") == session_id:
                        os.kill(meta["pid"], signal.SIGINT)
                        break
                except Exception:
                    continue
        except Exception:
            pass
        await ws.send(json.dumps({
            "type": "response",
            "request_id": request_id,
            "data": {"ok": True},
        }))


def _read_session_messages(jsonl_path: str, offset: int = 0, limit: int = 200) -> list[dict]:
    """Read user/assistant messages from a Claude session JSONL file.

    Transforms raw JSONL entries into the format the web dashboard expects.
    """
    import json
    from pathlib import Path

    p = Path(jsonl_path)
    if not p.exists():
        return []

    messages = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        entry_type = entry.get("type")
        if entry_type not in ("user", "assistant"):
            continue

        msg = entry.get("message", {})
        role = msg.get("role", entry_type)
        raw_content = msg.get("content", "")
        timestamp = entry.get("timestamp", "")

        # Transform content to dashboard ContentBlock format
        if isinstance(raw_content, str):
            content = [{"type": "TextBlock", "text": raw_content}]
        elif isinstance(raw_content, list):
            content = []
            for block in raw_content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type", "")
                if bt == "text":
                    content.append({"type": "TextBlock", "text": block.get("text", "")})
                elif bt == "thinking":
                    content.append({"type": "ThinkingBlock", "thinking": block.get("thinking", "")})
                elif bt == "tool_use":
                    content.append({
                        "type": "ToolUseBlock",
                        "tool_name": block.get("name", ""),
                        "tool_input": block.get("input", {}),
                        "tool_id": block.get("id", ""),
                    })
                elif bt == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        result_content = "\n".join(
                            c.get("text", "") for c in result_content if isinstance(c, dict)
                        )
                    content.append({
                        "type": "ToolResultBlock",
                        "content": result_content,
                        "is_error": block.get("is_error", False),
                    })
        else:
            content = []

        messages.append({
            "type": "UserPrompt" if role == "user" else "AssistantMessage",
            "role": role,
            "timestamp": timestamp,
            "content": content,
            "model": msg.get("model", ""),
        })

    return messages[offset : offset + limit]


# --------------------------------------------------------------------------
# cfleet leave
# --------------------------------------------------------------------------

@app.command()
def leave():
    """Disconnect this machine from the fleet server."""
    from cfleet.config import FleetConfig

    try:
        cfg = FleetConfig.load()
    except FileNotFoundError:
        console.print("[red]Not configured. Nothing to leave.[/red]")
        raise typer.Exit(1)

    old_url = cfg.server.url
    cfg.server.url = ""
    cfg.server.token = ""
    cfg.save()

    if old_url:
        console.print(f"[green]Left fleet at {old_url}. Server URL and token cleared.[/green]")
    else:
        console.print("[dim]No fleet connection configured.[/dim]")


# --------------------------------------------------------------------------
# cfleet gh setup
# --------------------------------------------------------------------------

@gh_app.command("setup")
def gh_setup():
    """Configure GitHub App credentials for token brokering.

    You need a GitHub App with the permissions you want to grant workers.
    The app's private key stays on the server — workers never see it.
    """
    from pathlib import Path
    from cfleet.config import FleetConfig, FleetState, FLEET_DIR

    try:
        cfg = FleetConfig.load()
    except FileNotFoundError:
        console.print("[red]Run 'cfleet init' first.[/red]")
        raise typer.Exit(1)

    console.print("\n[bold]GitHub App Setup[/bold]")
    console.print("[dim]Create a GitHub App at: https://github.com/settings/apps/new[/dim]")
    console.print("[dim]Recommended permissions: contents (rw), pull_requests (w), issues (w), metadata (r)[/dim]")
    console.print("[dim]Install it on your repos, then provide the details below.[/dim]\n")

    cfg.github.app_id = typer.prompt("GitHub App ID", default=cfg.github.app_id or "")
    cfg.github.installation_id = typer.prompt("Installation ID", default=cfg.github.installation_id or "")

    pem_path = FLEET_DIR / "github-app.pem"
    _import_private_key(pem_path)

    cfg.github.private_key_path = str(pem_path)
    cfg.save()

    console.print(f"\n[green]GitHub App configured:[/green]")
    console.print(f"  App ID:          {cfg.github.app_id}")
    console.print(f"  Installation ID: {cfg.github.installation_id}")
    console.print(f"  Private key:     {pem_path}")

    # Check branch protection for any existing write-level workers
    state = FleetState.load()
    write_repos = {r for w in state.workers.values() if w.github_level == "write" for r in w.repos}
    if write_repos and pem_path.exists():
        console.print(f"\n[bold]Branch protection check[/bold]:")
        _warn_branch_protection(sorted(write_repos))

    console.print()


def _import_private_key(pem_path) -> None:
    """Prompt user to provide a GitHub App private key if one doesn't exist yet."""
    from pathlib import Path

    if pem_path.exists():
        console.print(f"[dim]Private key: {pem_path}[/dim]")
        return

    console.print(f"\n[dim]Place your .pem file at: {pem_path}[/dim]")
    source = typer.prompt("Private key path (Enter to skip)", default="")
    if not source:
        console.print(f"[yellow]No key provided. Place it at {pem_path} before using GitHub features.[/yellow]")
        return

    source_path = Path(source).expanduser()
    if not source_path.exists():
        console.print(f"[yellow]File not found: {source_path}[/yellow]")
        return

    import shutil
    shutil.copy(source_path, pem_path)
    pem_path.chmod(0o600)
    console.print(f"Copied to {pem_path}")


# --------------------------------------------------------------------------
# cfleet gh set
# --------------------------------------------------------------------------

@gh_app.command("set")
def gh_set(
    worker_name: str = typer.Argument(..., help="Worker name"),
    level: str = typer.Argument(..., help="Access level: none, read, triage, or write"),
):
    """Set a worker's GitHub access level. Takes effect on next token renewal."""
    from cfleet.config import FleetState, GitHubLevel

    _validate_enum(level, GitHubLevel, "level")

    state = FleetState.load()
    if worker_name not in state.workers:
        console.print(f"[red]Worker '{worker_name}' not found.[/red]")
        raise typer.Exit(1)

    _set_worker_gh_level(worker_name, level)


# --------------------------------------------------------------------------
# cfleet gh get
# --------------------------------------------------------------------------

@gh_app.command("get")
def gh_get(
    worker_name: str = typer.Argument(..., help="Worker name"),
):
    """Show a worker's current GitHub access level."""
    from cfleet.config import FleetState, GH_PERMISSION_MAP, GitHubLevel

    state = FleetState.load()
    if worker_name not in state.workers:
        console.print(f"[red]Worker '{worker_name}' not found.[/red]")
        raise typer.Exit(1)

    worker = state.workers[worker_name]
    level = GitHubLevel(worker.github_level)
    perms = GH_PERMISSION_MAP.get(level, {})

    console.print(f"\n[bold]{worker_name}[/bold]")
    console.print(f"  GitHub level: [bold]{level.value}[/bold]")
    if perms:
        perm_str = ", ".join(f"{k}:{v}" for k, v in perms.items())
        console.print(f"  Permissions:  {perm_str}")
    if worker.repos:
        console.print(f"  Repos:        {', '.join(worker.repos)}")
    else:
        console.print("  Repos:        [dim]all installed repos[/dim]")
    console.print()


# --------------------------------------------------------------------------
# cfleet gh log
# --------------------------------------------------------------------------

@gh_app.command("log")
def gh_log(
    worker_name: Optional[str] = typer.Option(None, "--worker", "-w", help="Filter by worker name"),
    limit: int = typer.Option(20, "--limit", "-n", help="Number of entries to show"),
):
    """Show the GitHub token issuance audit log."""
    from cfleet.config import FleetState

    state = FleetState.load()
    entries = state.github_token_log
    if worker_name:
        entries = [e for e in entries if e.worker_name == worker_name]
    entries = entries[-limit:]

    if not entries:
        console.print("No token issuance records.")
        return

    table = Table(title="GitHub Token Log")
    table.add_column("Time", style="dim")
    table.add_column("Worker", style="bold")
    table.add_column("Level")
    table.add_column("Repos")
    table.add_column("Expires")

    level_colors = {"read": "green", "triage": "cyan", "write": "yellow"}

    for e in entries:
        ts = e.timestamp[:19].replace("T", " ") if e.timestamp else "-"
        exp = e.expires_at[:19].replace("T", " ") if e.expires_at else "-"
        color = level_colors.get(e.level, "white")
        repos = ", ".join(e.repos) if e.repos else "all"
        table.add_row(ts, e.worker_name, f"[{color}]{e.level}[/{color}]", repos, exp)

    console.print(table)
