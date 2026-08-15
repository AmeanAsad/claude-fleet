"""Typer CLI — all cfleet commands."""

from __future__ import annotations

import os
from pathlib import Path
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

operator_app = typer.Typer(help="Operator keys — individually revocable credentials for the operator API.")
app.add_typer(operator_app, name="operator")

secret_app = typer.Typer(help="Server-side canonical secrets (Anthropic key, default model).")
app.add_typer(secret_app, name="secret")

auth_app = typer.Typer(help="Toggle this host between Anthropic API-key and Claude account (OAuth) auth for workers.")
app.add_typer(auth_app, name="auth")

console = Console()


# ---------------------------------------------------------------------------
# Thin HTTP client for the central server
# ---------------------------------------------------------------------------

def _api_request(method: str, path: str, *, body: dict | None = None, role: str = "any") -> dict:
    """Send an authenticated request to the configured fleet server.

    `role` is a hint for which credential to prefer when both are present:
      - 'operator' : prefer server.operator_key, fall back to server.token
      - 'joiner'   : prefer server.joiner_token, fall back to server.token
      - 'any'      : use whichever is non-empty, operator first

    Exits with a clear message if no server is configured or the request fails.
    """
    import json as _json
    import urllib.error
    import urllib.request
    from cfleet.config import FleetConfig

    try:
        cfg = FleetConfig.load()
    except FileNotFoundError:
        console.print("[red]No fleet config found. Run 'cfleet init' or 'cfleet connect' first.[/red]")
        raise typer.Exit(1)

    if not cfg.server.url:
        console.print("[red]No server URL set. Run 'cfleet connect <url>' first.[/red]")
        raise typer.Exit(1)

    if role == "operator":
        token = cfg.server.operator_key or cfg.server.token
    elif role == "joiner":
        token = cfg.server.joiner_token or cfg.server.token
    else:
        token = cfg.server.operator_key or cfg.server.joiner_token or cfg.server.token

    if not token:
        console.print("[red]No credential configured for the fleet server.[/red]")
        raise typer.Exit(1)

    url = f"{cfg.server.url.rstrip('/')}{path}"
    data = _json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            return _json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = _json.loads(detail).get("detail", detail)
        except Exception:
            pass
        console.print(f"[red]Server returned {e.code}: {detail}[/red]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Could not reach server: {e}[/red]")
        raise typer.Exit(1)


def _engine():
    """Lazy-load engine to avoid import overhead on --help."""
    from cfleet.engine import FleetEngine
    return FleetEngine()


def _use_remote_server() -> bool:
    """True if this host is configured to talk to a fleet server (operator or joiner)."""
    from cfleet.config import FleetConfig
    try:
        cfg = FleetConfig.load()
    except FileNotFoundError:
        return False
    if not cfg.server.url:
        return False
    return bool(cfg.server.operator_key or cfg.server.joiner_token or cfg.server.token)


def _resolve_anthropic_key_fresh(cfg) -> str:
    """Return the most-recent Anthropic API key available to this host.

    Order:
      1. ANTHROPIC_API_KEY env var (explicit override always wins)
      2. Fresh fetch from server's /api/config/bootstrap if we have a credential
      3. Cached value in local config (secrets.anthropic_api_key, then legacy field)

    Best-effort: silently falls through on any network/HTTP error so workers
    don't fail to start when the server is briefly unreachable.
    """
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key:
        return env_key

    cred = cfg.server.operator_key or cfg.server.joiner_token or cfg.server.token
    if cfg.server.url and cred:
        try:
            import json as _json
            import urllib.request

            req = urllib.request.Request(
                f"{cfg.server.url.rstrip('/')}/api/config/bootstrap",
                headers={"Authorization": f"Bearer {cred}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read())
            fresh = data.get("anthropic_api_key", "")
            if fresh:
                return fresh
        except Exception:
            pass  # fall through to cached

    return cfg.resolve_anthropic_key()


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


def _wire_local_git_credential_helper(worker_name: str, cwd: str) -> None:
    """No-op kept for callers; `cfleet agent` now passes the credential helper
    inline via GIT_CONFIG_* env vars instead of editing user-global gitconfig.

    Editing the user's global gitconfig hijacked every github.com git
    operation on the host (Issue #N): when CFLEET_SERVER_URL wasn't set in
    the calling shell, the helper failed and git fell through to prompting
    for a password. Process-scoped config via env vars avoids all that.
    """
    return


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
    """List all machines (queries the connected server if there is one)."""
    if _use_remote_server():
        rows = _api_request("GET", "/api/machines")
        machines = [
            {
                "name": m.get("name", ""),
                "provider": m.get("provider", ""),
                "ip": m.get("ip", ""),
                "hostname": m.get("hostname", ""),
                "container_id": m.get("container_id", ""),
                "region": m.get("region", ""),
                "instance_type": m.get("instance_type", ""),
                "worker_names": m.get("worker_names", []),
                "status": m.get("status", ""),
                "connected": m.get("connected", False),
            }
            for m in (rows or [])
        ]
    else:
        engine = _engine()
        machines = [
            {
                "name": m.name,
                "provider": m.provider,
                "ip": m.ip,
                "hostname": m.hostname,
                "container_id": m.container_id,
                "region": m.region,
                "instance_type": m.instance_type,
                "worker_names": m.worker_names,
                "status": m.status,
                "connected": None,
            }
            for m in engine.list_machines()
        ]

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
        color = status_colors.get(m["status"], "white")
        ip_display = m["ip"] or m["hostname"] or (m["container_id"][:12] if m["container_id"] else "-")
        workers_display = ", ".join(m["worker_names"]) if m["worker_names"] else "-"
        status = m["status"]
        if m["connected"] is False and status == "ready":
            status = "disconnected"
        table.add_row(
            m["name"],
            m["provider"],
            ip_display,
            m["region"] or "-",
            m["instance_type"] or "-",
            workers_display,
            f"[{color}]{status}[/{color}]",
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


@machine_app.command("agent")
def machine_agent_cmd(
    name: Optional[str] = typer.Option(None, "--name", help="Machine name (defaults to hostname)"),
    server_url: Optional[str] = typer.Option(None, "--server-url", "-s", help="Fleet server URL"),
    token: Optional[str] = typer.Option(None, "--token", "-t", help="Fleet server token"),
):
    """Run the machine-agent daemon. Registers this host with the fleet server
    and handles spawn/kill commands for workers running on it.

    Usually started via systemd (set up by `cfleet machine create` on cloud VMs)
    or manually via `cfleet join` on a laptop.
    """
    import asyncio
    import platform
    from cfleet.config import FleetConfig
    from cfleet.machine_agent import MachineAgent

    try:
        cfg = FleetConfig.load()
    except FileNotFoundError:
        console.print("[red]Run 'cfleet join <server-url>' first.[/red]")
        raise typer.Exit(1)

    effective_server_url = server_url or cfg.server.url
    effective_token = token or cfg.server.token
    effective_name = name or platform.node()

    if not effective_server_url or not effective_token:
        console.print("[red]Missing server URL or token. Run 'cfleet join' or pass --server-url/--token.[/red]")
        raise typer.Exit(1)

    api_key = _resolve_anthropic_key_fresh(cfg)
    if not api_key:
        # Not fatal: prime-agent workers authenticate through prime-agent's own
        # per-machine config, not this key. Claude workers spawned without a key
        # will fail at first turn with an auth error.
        console.print(
            "[yellow]ANTHROPIC_API_KEY missing — claude workers will fail to authenticate. "
            "prime-agent workers are unaffected.[/yellow]"
        )

    agent = MachineAgent(
        server_url=effective_server_url,
        token=effective_token,
        machine_name=effective_name,
        api_key=api_key,
        model=cfg.model,
    )
    console.print(f"Starting machine-agent [bold]{effective_name}[/bold] -> {effective_server_url}")
    asyncio.run(agent.run())


# --------------------------------------------------------------------------
# cfleet machine doctor — Pulumi state drift diagnosis + repair
# --------------------------------------------------------------------------

doctor_app = typer.Typer(help="Diagnose and repair Pulumi state drift.")
machine_app.add_typer(doctor_app, name="doctor")


def _doctor_summarize() -> tuple[list[dict], dict]:
    """Return (resources_in_pulumi, fleet_state_machines)."""
    from cfleet.config import FleetState
    from cfleet.infra import InfraManager
    from cfleet.config import FleetConfig

    cfg = FleetConfig.load()
    infra = InfraManager(cfg)
    resources = infra.list_state_resources()
    state = FleetState.load()
    return resources, state.machines


@doctor_app.callback(invoke_without_command=True)
def doctor_root(ctx: typer.Context):
    """Show drift between Pulumi state and cfleet state.

    Reports resources that exist in one place but not the other. Run a
    subcommand to actually fix drift:
      refresh        — pulumi refresh (reconcile with cloud reality)
      cancel         — release a stuck Pulumi lock
      purge-state    — remove an orphan resource from Pulumi state by URN
    """
    if ctx.invoked_subcommand is not None:
        return

    try:
        resources, machines = _doctor_summarize()
    except Exception as e:
        console.print(f"[red]Could not read state: {e}[/red]")
        raise typer.Exit(1)

    # Index resources by machine name where possible.
    by_machine: dict[str, list[dict]] = {}
    other: list[dict] = []
    for r in resources:
        name = r.get("name", "")
        # The pulumi program prefixes per-machine resources with the machine name.
        matched = None
        for mname in machines:
            if name.startswith(f"{mname}-") or name == f"{mname}-vm" or name == mname:
                matched = mname
                break
        if matched:
            by_machine.setdefault(matched, []).append(r)
        else:
            other.append(r)

    table = Table(title="Pulumi vs cfleet drift")
    table.add_column("Machine")
    table.add_column("In cfleet")
    table.add_column("In Pulumi")
    table.add_column("Drift")

    for mname, m in machines.items():
        in_cfleet = "yes"
        in_pulumi = "yes" if mname in by_machine else "no"
        drift = (
            "—"
            if (in_pulumi == "yes" and m.provider in ("azure", "gcp"))
            or (in_pulumi == "no" and m.provider in ("external", "devcontainer"))
            else "[yellow]missing from Pulumi[/yellow]"
            if m.provider in ("azure", "gcp")
            else "[dim]non-cloud[/dim]"
        )
        table.add_row(mname, in_cfleet, in_pulumi, drift)

    for r in other:
        table.add_row(
            f"[dim]{r['name']}[/dim]",
            "no",
            "yes",
            "[red]orphan in Pulumi[/red]",
        )

    if not machines and not resources:
        console.print("[dim]No machines or Pulumi resources.[/dim]")
        return

    console.print(table)

    orphan_urns = [r["urn"] for r in other]
    if orphan_urns:
        console.print()
        console.print("[yellow]Orphan Pulumi resources (in state but no cfleet record):[/yellow]")
        for urn in orphan_urns:
            console.print(f"  {urn}")
        console.print()
        console.print(
            "[dim]To drop them from Pulumi state (use only if you're sure the real "
            "infra is gone):[/dim]"
        )
        for urn in orphan_urns:
            console.print(f"  cfleet machine doctor purge-state '{urn}'")


@doctor_app.command("refresh")
def doctor_refresh():
    """Reconcile Pulumi state with cloud reality.

    Wraps `pulumi refresh`. Use after deleting resources out-of-band (gcloud
    console, az portal). Per-resource failures (e.g. expired creds for one
    provider) won't abort the whole run.
    """
    from cfleet.infra import InfraManager
    from cfleet.config import FleetConfig

    try:
        InfraManager(FleetConfig.load()).refresh()
        console.print("[green]Pulumi state refreshed.[/green]")
    except Exception as e:
        console.print(f"[red]Refresh failed: {e}[/red]")
        raise typer.Exit(1)


@doctor_app.command("cancel")
def doctor_cancel():
    """Release a stuck Pulumi lock.

    Use when a previous `pulumi up`/`destroy` was killed mid-flight and
    subsequent runs fail with `the stack is currently locked`.
    """
    from cfleet.infra import InfraManager
    from cfleet.config import FleetConfig

    try:
        InfraManager(FleetConfig.load()).cancel()
        console.print("[green]Pulumi lock released.[/green]")
    except Exception as e:
        console.print(f"[red]Cancel failed: {e}[/red]")
        raise typer.Exit(1)


@doctor_app.command("purge-state")
def doctor_purge_state(
    urn: str = typer.Argument(..., help="Pulumi URN to remove from state"),
):
    """Remove an orphan resource from Pulumi state without touching real infra.

    Use only when you've confirmed the underlying resource is already gone
    (or you've separately destroyed it with `gcloud`/`az`). The URN comes
    from `cfleet machine doctor`.
    """
    from cfleet.infra import InfraManager
    from cfleet.config import FleetConfig

    try:
        InfraManager(FleetConfig.load()).delete_from_state(urn)
        console.print(f"[green]Removed {urn} from Pulumi state.[/green]")
    except Exception as e:
        console.print(f"[red]Delete failed: {e}[/red]")
        raise typer.Exit(1)


@machine_app.command("doctor")
def machine_doctor(
    refresh: bool = typer.Option(True, "--refresh/--no-refresh", help="Run pulumi refresh first"),
    fix: bool = typer.Option(False, "--fix", help="Remove orphan records (cfleet side + Pulumi state)"),
    cancel: bool = typer.Option(False, "--cancel", help="Run pulumi cancel to clear stuck locks"),
):
    """Diagnose and repair drift between cfleet state and Pulumi state.

    Runs `pulumi refresh` to reconcile with reality, then reports:

      • Machines in cfleet state with no matching Pulumi resource (cfleet orphans)
      • Pulumi resources tagged for a machine that's not in cfleet (Pulumi orphans)

    With `--fix`, removes cfleet orphans from state and Pulumi orphans from
    Pulumi state (does NOT delete real cloud resources — use `cfleet machine rm`
    for that).

    With `--cancel`, runs `pulumi cancel` first to clear stuck operation locks.
    """
    from cfleet.config import FleetState
    engine = _engine()

    if cancel:
        console.print("[dim]Cancelling any in-progress Pulumi operation...[/dim]")
        try:
            engine.infra.cancel()
            console.print("[green]Cancelled.[/green]")
        except Exception as e:
            console.print(f"[yellow]pulumi cancel failed: {e}[/yellow]")

    if refresh:
        console.print("[dim]Running pulumi refresh (this may take a minute)...[/dim]")
        try:
            engine.infra.refresh()
            console.print("[green]Refresh complete.[/green]")
        except Exception as e:
            console.print(f"[yellow]Refresh had errors (some resources may be unreachable): {e}[/yellow]")

    state = FleetState.load()
    try:
        resources = engine.infra.list_state_resources()
    except Exception as e:
        console.print(f"[red]Could not read Pulumi state: {e}[/red]")
        raise typer.Exit(1)

    # Group Pulumi resources by the machine name encoded in their resource name.
    # The provisioner names resources like "<machine>-vm", so we match on prefix.
    cloud_providers = {"azure", "gcp"}
    machines_in_cfleet = {
        n: m for n, m in state.machines.items() if m.provider in cloud_providers
    }

    pulumi_machine_names: set[str] = set()
    for res in resources:
        rname = res.get("name", "")
        if rname.endswith("-vm"):
            pulumi_machine_names.add(rname[:-3])

    cfleet_orphans = sorted(set(machines_in_cfleet.keys()) - pulumi_machine_names)
    pulumi_orphans = sorted(pulumi_machine_names - set(machines_in_cfleet.keys()))

    table = Table(title="Drift report")
    table.add_column("Status")
    table.add_column("Where")
    table.add_column("Name")
    if not cfleet_orphans and not pulumi_orphans:
        table.add_row("[green]ok[/green]", "—", "all in sync")
    else:
        for n in cfleet_orphans:
            table.add_row("[yellow]orphan[/yellow]", "cfleet", n)
        for n in pulumi_orphans:
            table.add_row("[yellow]orphan[/yellow]", "pulumi", n)
    console.print(table)

    if not fix:
        if cfleet_orphans or pulumi_orphans:
            console.print("[dim]Re-run with --fix to remove orphan records.[/dim]")
        return

    # --fix path
    for n in cfleet_orphans:
        console.print(f"Removing cfleet record for [bold]{n}[/bold]...")
        state.remove_machine(n)
    if cfleet_orphans:
        state.save()

    for n in pulumi_orphans:
        # All resources whose URN ends with the machine prefix
        machine_resources = [r for r in resources if r["name"].startswith(f"{n}-")]
        for r in machine_resources:
            urn = r["urn"]
            console.print(f"Deleting from Pulumi state: [dim]{urn}[/dim]")
            try:
                engine.infra.delete_from_state(urn)
            except Exception as e:
                console.print(f"[yellow]  failed: {e}[/yellow]")

    console.print("[green]Doctor pass complete.[/green]")


# --------------------------------------------------------------------------
# cfleet spawn
# --------------------------------------------------------------------------

@app.command()
def spawn(
    name: str = typer.Argument(..., help="Worker name"),
    machine: Optional[str] = typer.Option(None, "--machine", "-M", help="Machine to spawn on (auto-creates if omitted)"),
    cwd: Optional[str] = typer.Option(None, "--cwd", help="Working directory on the target machine (external only)"),
    repo: list[str] = typer.Option([], "--repo", "-r", help="Repos to clone (repeatable, defaults to all)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override default model"),
    vm_type: Optional[str] = typer.Option(None, "--type", help="VM type for auto-created machine: regular, snp, or tdx"),
    instance_type: Optional[str] = typer.Option(None, "--instance-type", "-t", help="Override machine type/SKU (auto-create only)"),
    region: Optional[str] = typer.Option(None, "--region", help="Override default region (auto-create only)"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Provider for auto-created machine"),
    gh: Optional[str] = typer.Option(None, "--gh", help="GitHub access level: read, triage, or write"),
    skip_permissions: bool = typer.Option(True, "--skip-permissions/--no-skip-permissions", help="Run with --dangerously-skip-permissions (default: on)"),
    backend: str = typer.Option("claude", "--backend", help="Agent runtime: 'claude' (default) or 'prime' (prime-agent daemon session)"),
):
    """Spawn a new fleet worker on a machine."""
    from cfleet.config import GitHubLevel, VMType

    if gh:
        _validate_enum(gh, GitHubLevel, "--gh")
    if backend not in ("claude", "prime"):
        console.print(f"[red]Invalid --backend '{backend}'. Use 'claude' or 'prime'.[/red]")
        raise typer.Exit(1)

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
        cwd=cwd,
        skip_permissions=skip_permissions,
        agent_backend=backend,
    )

    if gh:
        _set_worker_gh_level(name, gh)


# --------------------------------------------------------------------------
# cfleet ls
# --------------------------------------------------------------------------

@app.command(name="ls")
def list_workers():
    """List all fleet workers (queries the connected server if there is one)."""
    if _use_remote_server():
        rows = _api_request("GET", "/api/workers")
        workers = [
            {
                "name": w.get("name", ""),
                "machine_name": w.get("machine_name", ""),
                "status": w.get("status", ""),
                "relay_port": w.get("relay_port", ""),
                "model": w.get("model", ""),
                "backend": w.get("agent_backend", "claude") or "claude",
                "last_prompt": w.get("last_prompt", ""),
                "connected": w.get("connected", False),
            }
            for w in (rows or [])
        ]
    else:
        engine = _engine()
        workers = [
            {
                "name": w.name,
                "machine_name": w.machine_name,
                "status": w.status,
                "relay_port": w.relay_port,
                "model": w.model,
                "backend": w.agent_backend or "claude",
                "last_prompt": w.last_prompt,
                "connected": None,
            }
            for w in engine.list_workers()
        ]

    if not workers:
        console.print("No workers. Run [bold]cfleet spawn <name>[/bold] to create one.")
        return

    table = Table(title="Fleet Workers")
    table.add_column("Name", style="bold")
    table.add_column("Machine")
    table.add_column("Status")
    table.add_column("Backend")
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
        color = status_colors.get(w["status"], "white")
        last = w["last_prompt"] or ""
        prompt_display = last[:50] + "..." if len(last) > 50 else (last or "-")
        status = w["status"]
        if w["connected"] is False:
            status = f"{status} (offline)"
        table.add_row(
            w["name"],
            w["machine_name"] or "-",
            f"[{color}]{status}[/{color}]",
            w["backend"],
            w["model"] or "-",
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
    if _use_remote_server():
        _api_request("POST", f"/api/workers/{name}/ask", body={"prompt": prompt})
        console.print(f"[green]Prompt sent to {name}[/green]")
        return
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
    if _use_remote_server():
        _api_request("POST", f"/api/workers/{name}/interrupt")
        console.print(f"[green]Interrupt sent to {name}[/green]")
        return
    engine = _engine()
    engine.interrupt(name)


# --------------------------------------------------------------------------
# cfleet attach
# --------------------------------------------------------------------------

@app.command()
def shell(
    name: str = typer.Argument(..., help="Worker name"),
):
    """Open a debug shell on a worker's underlying machine (SSH/docker exec)."""
    engine = _engine()
    engine.attach(name)


# --------------------------------------------------------------------------
# cfleet send
# --------------------------------------------------------------------------

def _resolve_worker_ssh(name: str) -> tuple[str, str, str, str]:
    """Look up a worker's SSH details, preferring the remote server when connected.

    Returns (ssh_host, ssh_user, cwd, ssh_key_path).
    """
    from cfleet.config import FleetConfig
    cfg = FleetConfig.load()

    if _use_remote_server():
        worker = _api_request("GET", f"/api/workers/{name}")
        ssh_host = worker.get("ssh_host", "")
        ssh_user = worker.get("ssh_user", "")
        cwd = worker.get("cwd", "")
        if not ssh_host:
            machine_name = worker.get("machine_name", "")
            if machine_name:
                machines = _api_request("GET", "/api/machines")
                for m in machines:
                    if m["name"] == machine_name:
                        ssh_host = m.get("ssh_host", "") or m.get("ip", "")
                        ssh_user = ssh_user or m.get("ssh_user", "")
                        break
        if not ssh_host:
            console.print(f"[red]No SSH host found for worker '{name}'.[/red]")
            raise typer.Exit(1)
        ssh_key = str(cfg.resolve_ssh_key())
        return ssh_host, ssh_user or "ubuntu", cwd, ssh_key

    engine = _engine()
    worker = engine.state.get_worker(name)
    machine = engine._get_machine_for_worker(worker)
    ssh_host = machine.ssh_host or machine.ip
    ssh_user = machine.ssh_user or cfg.resolve_ssh_user(provider=machine.provider)
    return ssh_host, ssh_user, worker.cwd, str(cfg.resolve_ssh_key())


@app.command()
def send(
    name: str = typer.Argument(..., help="Worker name"),
    local_path: str = typer.Argument(..., help="Local path to send"),
    to: Optional[str] = typer.Option(None, "--to", help="Remote destination path (defaults to worker cwd)"),
):
    """Send files to a worker via rsync (or `docker cp` for devcontainer)."""
    if _use_remote_server():
        ssh_host, ssh_user, cwd, ssh_key = _resolve_worker_ssh(name)
        dest = to or ((cwd.rstrip("/") + "/inbox/") if cwd else "/workspace/inbox/")
        from cfleet.ssh import rsync_to
        rsync_to(ssh_host, ssh_user, ssh_key, local_path, dest)
        console.print(f"Sent {local_path} to [bold]{name}[/bold]:{dest}")
        return
    engine = _engine()
    engine.send(name, local_path, to)


# --------------------------------------------------------------------------
# cfleet collect
# --------------------------------------------------------------------------

@app.command()
def collect(
    name: str = typer.Argument(..., help="Worker name"),
    local_dest: str = typer.Argument(..., help="Local destination path"),
    path: Optional[str] = typer.Option(None, "--path", help="Remote path to collect (defaults to worker cwd)"),
):
    """Collect files from a worker via rsync (or `docker cp` for devcontainer)."""
    if _use_remote_server():
        ssh_host, ssh_user, cwd, ssh_key = _resolve_worker_ssh(name)
        source = path or ((cwd.rstrip("/") + "/outbox/") if cwd else "/workspace/outbox/")
        from cfleet.ssh import rsync_from
        rsync_from(ssh_host, ssh_user, ssh_key, source, local_dest)
        console.print(f"Collected {source} from [bold]{name}[/bold] to {local_dest}")
        return
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
    purge_session: bool = typer.Option(False, "--purge-session", help="Also delete the worker's session JSONL on the host (conversation history is gone)"),
):
    """Destroy a worker (or all with --all).

    If the worker or machine is unreachable, use --purge to skip remote cleanup.
    --purge-session also removes the conversation history JSONL on the host so
    a future worker with the same name starts fresh.
    """
    if _use_remote_server():
        if all_workers:
            console.print("[red]--all is not supported in remote mode yet.[/red]")
            raise typer.Exit(1)
        if not name:
            console.print("[red]Provide a worker name or --all[/red]")
            raise typer.Exit(1)
        params = []
        if purge:
            params.append("purge=true")
        if purge_session:
            params.append("purge_session=true")
        qs = ("?" + "&".join(params)) if params else ""
        _api_request("DELETE", f"/api/workers/{name}{qs}")
        msg = f"Worker {name} destroyed"
        if purge_session:
            msg += " (session file removed)"
        console.print(f"[green]{msg}.[/green]")
        return

    engine = _engine()

    if all_workers:
        engine.kill_all(collect_path=collect_to)
    elif name:
        engine.kill(name, collect_path=collect_to, force=force, remove_machine=rm_machine, purge=purge, purge_session=purge_session)
    else:
        console.print("[red]Provide a worker name or --all[/red]")
        raise typer.Exit(1)


# --------------------------------------------------------------------------
# cfleet restart
# --------------------------------------------------------------------------

@app.command()
def restart(
    name: str = typer.Argument(..., help="Worker name"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Switch model on restart"),
):
    """Restart a worker: kill the process, respawn on the same machine.

    The conversation carries over — the worker directory holds a marker file
    with the session ID, so the new process resumes where the old one left off.
    Model can be changed on restart (it's a launch flag, not identity).
    """
    if _use_remote_server():
        # Fetch worker details to get machine_name, cwd, model, skip_permissions
        worker = _api_request("GET", f"/api/workers/{name}")
        machine_name = worker.get("machine_name", "")
        cwd = worker.get("cwd", "")
        effective_model = model or worker.get("model", "")
        skip_perms = worker.get("skip_permissions", True)

        if not machine_name:
            console.print(f"[red]Worker '{name}' has no machine.[/red]")
            raise typer.Exit(1)

        # Kill (preserve session)
        console.print(f"Stopping [bold]{name}[/bold]...")
        _api_request("DELETE", f"/api/workers/{name}")

        import time
        time.sleep(1)

        # Respawn on the same machine with the same cwd
        console.print(f"Respawning [bold]{name}[/bold] on {machine_name}...")
        result = _api_request("POST", f"/api/machines/{machine_name}/spawn", body={
            "worker_name": name,
            "model": effective_model,
            "cwd": cwd,
            "repos": [],
            "skip_permissions": skip_perms,
        })
        if result.get("error"):
            console.print(f"[red]Respawn failed: {result['error']}[/red]")
            raise typer.Exit(1)

        msg = f"Worker {name} restarted"
        if model:
            msg += f" with model {model}"
        console.print(f"[green]{msg}.[/green]")
        return

    engine = _engine()
    engine.restart(name, model=model)


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
    operator_key: Optional[str] = typer.Option(None, "--operator-key", help="Operator key (preferred)"),
    token: Optional[str] = typer.Option(None, "--token", "-t", help="Legacy single-token alias"),
    pull_secrets: bool = typer.Option(True, "--pull-secrets/--no-pull-secrets", help="Also pull canonical secrets (Anthropic key, default model) into local config"),
):
    """Point this CLI at a fleet server (operator mode).

    Saves the server URL + your operator key to ~/.cfleet/config.yml. Operator
    keys are issued via `cfleet operator add <name>` on a host that already
    authenticates to the server — typically the server's host itself, or
    another laptop you've already connected.

    With --pull-secrets (default), also fetches the canonical Anthropic key
    and default model from the server so the local config matches without
    you having to type them in again.

    For machines that should host workers, use `cfleet join` instead.
    """
    import json as _json
    import urllib.error
    import urllib.request
    from cfleet.config import FleetConfig, FLEET_DIR

    try:
        cfg = FleetConfig.load()
    except FileNotFoundError:
        FLEET_DIR.mkdir(parents=True, exist_ok=True)
        cfg = FleetConfig()

    server_url = server_url.rstrip("/")
    effective_key = operator_key or token or cfg.server.operator_key or cfg.server.token

    cfg.server.url = server_url
    if operator_key:
        cfg.server.operator_key = operator_key
        # Mirror into legacy field for older code paths that read server.token.
        cfg.server.token = operator_key
    elif token:
        cfg.server.token = token
        cfg.server.operator_key = token
    cfg.save()

    console.print(f"[green]Connected to server at {server_url}[/green]")
    if not effective_key:
        console.print(
            "[yellow]No credential set. Pass --operator-key (recommended) or --token.[/yellow]"
        )
        return

    if pull_secrets:
        try:
            req = urllib.request.Request(
                f"{server_url}/api/config/secrets",
                headers={"Authorization": f"Bearer {effective_key}"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read())
            if data.get("anthropic_api_key"):
                cfg.secrets.anthropic_api_key = data["anthropic_api_key"]
                cfg.anthropic_api_key = ""  # clear legacy so secrets is the source
            if data.get("model"):
                cfg.secrets.model = data["model"]
            cfg.save()
            console.print("[green]Pulled canonical secrets from server.[/green]")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                console.print(
                    f"[yellow]Skipped secret pull: server returned {e.code}. "
                    f"Likely this is a joiner-only credential — use 'cfleet join' "
                    f"on host machines, or issue an operator key.[/yellow]"
                )
            else:
                detail = e.read().decode(errors="replace")
                console.print(f"[yellow]Skipped secret pull: server returned {e.code}: {detail}[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Skipped secret pull: {e}[/yellow]")


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
# cfleet operator — manage per-operator keys against the server
# --------------------------------------------------------------------------

@operator_app.command("add")
def operator_add(
    name: str = typer.Argument(..., help="Friendly name for this operator (e.g. 'amean-laptop')"),
):
    """Mint a new operator key on the server. Prints the raw key ONCE.

    Save the printed key somewhere safe — the server only stores the hash, so
    if you lose it you must `cfleet operator rm <name>` and re-issue.
    """
    resp = _api_request("POST", "/api/admin/operators", body={"name": name}, role="operator")
    key = resp.get("key", "")
    console.print(f"[green]Issued operator key[/green] '[bold]{name}[/bold]'")
    console.print(f"  [bold]{key}[/bold]")
    console.print("[dim]This is the only time you'll see the raw key. Save it now.[/dim]")
    console.print(
        f"[dim]On the receiving machine: cfleet connect <server-url> --operator-key {key}[/dim]"
    )


@operator_app.command("ls")
def operator_ls():
    """List operator keys registered on the server (names + created_at only)."""
    rows = _api_request("GET", "/api/admin/operators", role="operator")
    if not rows:
        console.print("[dim]No operator keys issued yet.[/dim]")
        return
    table = Table()
    table.add_column("Name")
    table.add_column("Created at")
    for r in rows:
        table.add_row(r.get("name", ""), r.get("created_at", ""))
    console.print(table)


@operator_app.command("rm")
def operator_rm(
    name: str = typer.Argument(..., help="Operator name to revoke"),
):
    """Revoke an operator key. Takes effect immediately on the server."""
    _api_request("DELETE", f"/api/admin/operators/{name}", role="operator")
    console.print(f"[green]Revoked operator key '[bold]{name}[/bold]'.[/green]")


# --------------------------------------------------------------------------
# cfleet secret — rotate the canonical Anthropic key on the server
# --------------------------------------------------------------------------

@secret_app.command("set")
def secret_set(
    key: str = typer.Argument(..., help="Secret name (currently only 'anthropic')"),
    value: str = typer.Argument(..., help="The new secret value"),
):
    """Rotate a canonical secret on the server.

    Supports 'anthropic' and 'kimi'. New `cfleet agent` spawns on every worker
    pick up the rotated value automatically; in-flight processes keep their
    existing env until restarted.
    """
    if key in {"anthropic", "anthropic_api_key"}:
        _api_request(
            "PUT",
            "/api/config/secrets/anthropic_api_key",
            body={"anthropic_api_key": value},
            role="operator",
        )
        console.print("[green]Anthropic key rotated.[/green]")
    elif key in {"kimi", "kimi_api_key"}:
        _api_request(
            "PUT",
            "/api/config/secrets/kimi_api_key",
            body={"kimi_api_key": value},
            role="operator",
        )
        console.print("[green]Kimi API key set.[/green]")
    else:
        console.print(f"[red]Unknown secret '{key}'. Supported: anthropic, kimi[/red]")
        raise typer.Exit(1)
    console.print("[dim]Existing workers keep the old key until restarted.[/dim]")


@secret_app.command("get")
def secret_get():
    """Pull the canonical secrets from the server into local config.

    Same effect as `cfleet connect --pull-secrets`. Useful after rotation if
    you want to refresh local state without reconnecting.
    """
    from cfleet.config import FleetConfig

    data = _api_request("GET", "/api/config/secrets", role="operator")
    cfg = FleetConfig.load()
    if data.get("anthropic_api_key"):
        cfg.secrets.anthropic_api_key = data["anthropic_api_key"]
    if data.get("kimi_api_key"):
        cfg.secrets.kimi_api_key = data["kimi_api_key"]
    if data.get("model"):
        cfg.secrets.model = data["model"]
    cfg.save()
    console.print("[green]Pulled secrets from server.[/green]")


# --------------------------------------------------------------------------
# cfleet join — daemon helpers
# --------------------------------------------------------------------------

def _has_systemd() -> bool:
    """True iff systemd is the active init on this host (Linux only)."""
    import platform
    import shutil as _shutil
    if platform.system() != "Linux":
        return False
    if not _shutil.which("systemctl"):
        return False
    # systemctl exists on macOS as a noop wrapper in some homebrew installs;
    # `is-system-running` is a definitive runtime check.
    import subprocess
    try:
        subprocess.run(
            ["systemctl", "is-system-running"],
            capture_output=True,
            timeout=3,
            check=False,
        )
        return True
    except Exception:
        return False


def _install_machine_agent_systemd(machine_name: str, ssh_host: str = "", ssh_user: str = "") -> bool:
    """Install + enable cfleet-machine-agent.service. Returns True on success.

    Uses sudo if the current user isn't root. Idempotent.
    """
    import shutil as _shutil
    import subprocess

    cfleet_bin = _shutil.which("cfleet") or "/usr/local/bin/cfleet"
    user = os.environ.get("USER", "")
    home = os.environ.get("HOME", "")

    extra_env = ""
    if ssh_host:
        extra_env += f"Environment=CFLEET_SSH_HOST={ssh_host}\n"
    if ssh_user:
        extra_env += f"Environment=USER={ssh_user}\n"

    unit = f"""\
[Unit]
Description=Claude Fleet machine agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={home}
Environment=HOME={home}
Environment=PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin
{extra_env}ExecStart={cfleet_bin} machine agent --name {machine_name}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

    sudo = [] if os.geteuid() == 0 else ["sudo"]
    try:
        # Write the unit
        p = subprocess.run(
            [*sudo, "tee", "/etc/systemd/system/cfleet-machine-agent.service"],
            input=unit,
            text=True,
            capture_output=True,
        )
        if p.returncode != 0:
            console.print(f"[yellow]Could not write systemd unit: {p.stderr.strip()}[/yellow]")
            return False
        subprocess.run([*sudo, "systemctl", "daemon-reload"], check=True, capture_output=True)
        subprocess.run(
            [*sudo, "systemctl", "enable", "--now", "cfleet-machine-agent.service"],
            check=True,
            capture_output=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        console.print(f"[yellow]systemd setup failed: {e.stderr.decode() if e.stderr else e}[/yellow]")
        return False


# --------------------------------------------------------------------------
# cfleet join
# --------------------------------------------------------------------------

@app.command()
def join(
    server_url: str = typer.Argument(..., help="Server URL (e.g. http://my-server:8420)"),
    joiner_token: Optional[str] = typer.Option(None, "--joiner-token", help="Joiner token (machines/workers credential)"),
    token: Optional[str] = typer.Option(None, "--token", "-t", help="Alias for --joiner-token (legacy)"),
    api_key: Optional[str] = typer.Option(None, "--api-key", help="Override Anthropic API key (default: pull from server)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override default model (default: pull from server)"),
    name: Optional[str] = typer.Option(None, "--name", help="Machine name (defaults to hostname)"),
    ssh_host: Optional[str] = typer.Option(None, "--ssh-host", help="Public SSH target so operators can `cfleet attach` (auto-detects tailscale IP if omitted)"),
    ssh_user: Optional[str] = typer.Option(None, "--ssh-user", help="SSH login user (defaults to $USER)"),
    skip_bootstrap: bool = typer.Option(False, "--skip-bootstrap", help="Skip system deps install"),
    skip_daemon: bool = typer.Option(False, "--skip-daemon", help="Skip starting the machine-agent daemon"),
):
    """Register this machine as a fleet host.

    Saves the server URL + joiner token to ~/.cfleet/config.yml, pulls the
    canonical Anthropic API key + default model from the server's bootstrap
    endpoint, then starts the machine-agent daemon so the server can spawn
    workers on this host.

    The Anthropic key is *not* prompted for or supplied via flag in the
    normal flow — the server is the canonical store. Override with --api-key
    only if you need a different key for this host specifically.

    For a CLI-only operator setup (no daemon, no joiner role), use
    `cfleet connect` instead.
    """
    import json as _json
    import platform
    import urllib.error
    import urllib.request
    from cfleet.config import FleetConfig, FLEET_DIR

    try:
        cfg = FleetConfig.load()
    except FileNotFoundError:
        FLEET_DIR.mkdir(parents=True, exist_ok=True)
        cfg = FleetConfig()

    effective_token = joiner_token or token
    if not effective_token:
        console.print("[red]--joiner-token (or legacy --token) is required.[/red]")
        raise typer.Exit(1)

    server_url = server_url.rstrip("/")

    # Pull canonical secrets from the server using the joiner credential.
    pulled_api_key = ""
    pulled_model = ""
    try:
        req = urllib.request.Request(
            f"{server_url}/api/config/bootstrap",
            headers={"Authorization": f"Bearer {effective_token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())
        pulled_api_key = data.get("anthropic_api_key", "")
        pulled_kimi_key = data.get("kimi_api_key", "")
        pulled_model = data.get("model", "")
        console.print(f"[green]Pulled bootstrap config from {server_url}[/green]")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        console.print(f"[red]Server returned {e.code}: {detail}[/red]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Could not fetch bootstrap config: {e}[/red]")
        raise typer.Exit(1)

    effective_api_key = api_key or pulled_api_key or cfg.resolve_anthropic_key() or os.environ.get("ANTHROPIC_API_KEY", "")
    if not effective_api_key:
        console.print("[red]No Anthropic API key returned from server and none configured locally.[/red]")
        raise typer.Exit(1)

    effective_model = model or pulled_model or cfg.resolve_model() or "claude-opus-4-6"

    if not skip_bootstrap:
        from cfleet.provisioner import local_bootstrap
        local_bootstrap(api_key=effective_api_key, model=effective_model)

    cfg.server.url = server_url
    cfg.server.joiner_token = effective_token
    # Mirror into the legacy field so older code that reads server.token keeps working.
    cfg.server.token = effective_token
    cfg.secrets.anthropic_api_key = effective_api_key
    if pulled_kimi_key:
        cfg.secrets.kimi_api_key = pulled_kimi_key
    cfg.secrets.model = effective_model
    # Clear the legacy top-level field so resolve_anthropic_key returns the new one.
    cfg.anthropic_api_key = ""
    cfg.model = effective_model
    cfg.save()

    console.print(f"[green]Saved fleet config -> {server_url}[/green]")

    if skip_daemon:
        console.print("[dim]--skip-daemon set; start the agent manually with `cfleet machine agent`.[/dim]")
        return

    machine_name = name or platform.node()

    detected_host, detected_user = _detect_ssh_target()
    effective_ssh_host = ssh_host if ssh_host is not None else detected_host
    effective_ssh_user = ssh_user if ssh_user is not None else detected_user
    if effective_ssh_host:
        console.print(f"[dim]SSH target for attach: {effective_ssh_user}@{effective_ssh_host}[/dim]")
    else:
        console.print(
            "[yellow]No SSH host detected; `cfleet attach` from other machines won't work.[/yellow]\n"
            "[dim]Pass --ssh-host <reachable-ip-or-hostname> to enable it.[/dim]"
        )

    if _has_systemd():
        if _install_machine_agent_systemd(machine_name, ssh_host=effective_ssh_host, ssh_user=effective_ssh_user):
            console.print(f"[green]Started cfleet-machine-agent.service ({machine_name})[/green]")
            console.print("[dim]Check status:  sudo systemctl status cfleet-machine-agent.service[/dim]")
            console.print("[dim]Tail logs:     sudo journalctl -u cfleet-machine-agent -f[/dim]")
        else:
            console.print(
                "[yellow]Falling back to manual start; run:[/yellow]\n"
                f"  cfleet machine agent --name {machine_name}"
            )
    else:
        console.print(
            "[dim]No systemd detected. Start the agent in another terminal "
            "(or via launchd/screen/tmux):[/dim]\n"
            f"  cfleet machine agent --name {machine_name}"
        )


# --------------------------------------------------------------------------
# cfleet agent
# --------------------------------------------------------------------------

def _detect_ssh_target() -> tuple[str, str]:
    """Best-effort detection of an SSH host/user other machines could use to reach this one.

    Resolution order for the host:
      1. CFLEET_SSH_HOST env var (operator override)
      2. tailscale IPv4 (if tailscale is installed and up)
      3. empty (laptop behind NAT — `cfleet attach` will print a helpful message)

    Returns ("", "") when nothing usable is detected.
    """
    import getpass
    import subprocess

    user = os.environ.get("USER") or getpass.getuser() or ""
    host = os.environ.get("CFLEET_SSH_HOST", "").strip()
    if host:
        return host, user

    try:
        p = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=2,
        )
        if p.returncode == 0:
            ts_ip = p.stdout.strip().splitlines()[0].strip() if p.stdout.strip() else ""
            if ts_ip:
                return ts_ip, user
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return "", user


@app.command()
def agent(
    name: str = typer.Argument(..., help="Worker name"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override model"),
    cwd: Optional[str] = typer.Option(None, "--cwd", help="Working directory"),
    server_url: Optional[str] = typer.Option(None, "--server-url", "-s", help="Server URL (reads from config if omitted)"),
    token: Optional[str] = typer.Option(None, "--token", "-t", help="Server token"),
    ssh_host: Optional[str] = typer.Option(None, "--ssh-host", help="Public SSH target (host[:port]) others can reach this machine on"),
    ssh_user: Optional[str] = typer.Option(None, "--ssh-user", help="SSH login user (defaults to $USER)"),
    skip_permissions: bool = typer.Option(True, "--skip-permissions/--no-skip-permissions", help="Run with --dangerously-skip-permissions (default: on)"),
    session_id_override: Optional[str] = typer.Option(None, "--session-id", help="Resume an existing session (used internally by machine agent on respawn)"),
    backend: str = typer.Option("claude", "--backend", help="Agent runtime: 'claude' (Claude Code SDK) or 'prime' (prime-agent daemon session)"),
):
    """Start a headless worker registered with the fleet.

    With the default claude backend, runs the Claude Code SDK in-process and
    connects to the fleet server via WebSocket. With --backend prime, drives a
    resident prime-agent daemon session instead. The dashboard is the primary
    UI — prompts sent there execute here. Use `cfleet attach <name>` to drop
    into a TUI on the same session.
    """
    import asyncio
    import platform
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

    # Default cwd to $HOME/<worker_name>/ so every worker — manual launches
    # included — gets its own dir. Matches machine_agent._handle_spawn.
    if cwd:
        workspace = cwd
    else:
        workspace = str(Path.home() / name)

    if not effective_server_url:
        console.print("[red]No server URL. Pass --server-url or run 'cfleet join' first.[/red]")
        raise typer.Exit(1)

    if not effective_token:
        console.print("[red]No server token. Pass --token or set it in config.yml[/red]")
        raise typer.Exit(1)

    workspace = str(Path(workspace).resolve())
    Path(workspace).mkdir(parents=True, exist_ok=True)

    # Scaffold inbox/outbox/repos + CLAUDE.md so manual `cfleet agent` and
    # machine-agent-driven spawns produce identical workspaces.
    wd_path = Path(workspace)
    for sub in ("inbox", "outbox", "repos"):
        (wd_path / sub).mkdir(exist_ok=True)
    claude_md_dst = wd_path / "CLAUDE.md"
    if not claude_md_dst.exists():
        try:
            claude_md_src = cfg.resolve_claude_md()
        except Exception:
            claude_md_src = None
        if not claude_md_src or not claude_md_src.exists():
            claude_md_src = Path(__file__).parent / "defaults" / "CLAUDE.md"
        if claude_md_src.exists():
            import shutil as _sh
            _sh.copy(claude_md_src, claude_md_dst)

    # Provider env: third-party models (kimi-*) need ANTHROPIC_BASE_URL and
    # their own API key regardless of auth mode.
    from cfleet.config import resolve_provider_env as _resolve_prov
    _provider_env = _resolve_prov(effective_model, cfg)

    if _provider_env:
        for k, v in _provider_env.items():
            os.environ[k] = v
    else:
        # Native Claude model — respect host auth mode.
        try:
            _auth_mode = (Path.home() / ".cfleet" / "auth-mode").read_text().strip()
        except FileNotFoundError:
            _auth_mode = ""
        if _auth_mode == "oauth":
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("CLAUDE_CODE_API_KEY", None)
        else:
            api_key = _resolve_anthropic_key_fresh(cfg)
            if api_key:
                os.environ["ANTHROPIC_API_KEY"] = api_key

    # Surface fleet identity to child processes (git's credential.helper, etc.)
    os.environ["CFLEET_SERVER_URL"] = effective_server_url
    os.environ["CFLEET_TOKEN"] = effective_token
    os.environ["CFLEET_WORKER_NAME"] = name

    # Wire cfleet-gh-token as a credential helper for github.com — but only
    # for git processes descended from this `cfleet agent`. Using
    # GIT_CONFIG_COUNT/KEY/VALUE keeps it process-scoped; no user-global
    # gitconfig is touched, so `git pull` in unrelated repos isn't affected.
    #
    # We inject THREE entries: an empty `helper =` to reset any helpers from
    # the user's ~/.gitconfig (which often has `gh auth git-credential` wired
    # up for github.com — that would otherwise race ours and win), then our
    # helper, then useHttpPath so the helper sees the full repo URL.
    import shutil as _shutil
    gh_helper = _shutil.which("cfleet-gh-token")
    if gh_helper:
        existing_count = int(os.environ.get("GIT_CONFIG_COUNT", "0") or 0)
        os.environ["GIT_CONFIG_COUNT"] = str(existing_count + 3)
        os.environ[f"GIT_CONFIG_KEY_{existing_count}"] = "credential.https://github.com.helper"
        os.environ[f"GIT_CONFIG_VALUE_{existing_count}"] = ""
        os.environ[f"GIT_CONFIG_KEY_{existing_count + 1}"] = "credential.https://github.com.helper"
        os.environ[f"GIT_CONFIG_VALUE_{existing_count + 1}"] = gh_helper
        os.environ[f"GIT_CONFIG_KEY_{existing_count + 2}"] = "credential.https://github.com.useHttpPath"
        os.environ[f"GIT_CONFIG_VALUE_{existing_count + 2}"] = "true"

    # `gh` CLI shim: prepend a tiny wrapper to PATH that mints a fresh token
    # for every invocation. Avoids env-var staleness (GH App installation
    # tokens expire ~1h) and avoids touching ~/.config/gh.
    real_gh = _shutil.which("gh")
    if gh_helper and real_gh:
        shim_dir = Path(workspace).parent / ".cfleet-bin"
        shim_dir.mkdir(exist_ok=True)
        shim_path = shim_dir / "gh"
        shim_path.write_text(
            "#!/usr/bin/env bash\n"
            f'exec env GH_TOKEN="$(printf \'host=github.com\\n\\n\' | {gh_helper} get '
            "| awk -F= '/^password=/{print $2}')\" "
            f'{real_gh} "$@"\n'
        )
        shim_path.chmod(0o755)
        os.environ["PATH"] = f"{shim_dir}:{os.environ.get('PATH', '')}"

    # Marker file: the worker directory IS the identity. If a marker exists,
    # reuse its session_id so restarts resume the conversation. If not, this
    # is a fresh spawn — generate a new id and write the marker.
    # An explicit --session-id flag (from machine agent respawn) takes priority.
    marker_path = wd_path / ".cfleet-worker"
    existing_session_id = session_id_override
    if not existing_session_id and marker_path.exists():
        try:
            import json as _mj
            marker = _mj.loads(marker_path.read_text())
            existing_session_id = marker.get("session_id")
        except Exception:
            pass

    if backend == "prime":
        _run_prime_agent_worker(
            name=name,
            workspace=workspace,
            marker_path=marker_path,
            existing_session_id=existing_session_id,
            model=model or "",
            server_url=effective_server_url,
            token=effective_token,
            machine_name=machine_name,
            ssh_host=ssh_host or "",
            ssh_user=ssh_user or "",
        )
        return
    if backend != "claude":
        console.print(f"[red]Unknown backend '{backend}'. Use 'claude' or 'prime'.[/red]")
        raise typer.Exit(1)

    session_id = existing_session_id or str(uuid.uuid4())
    try:
        import json as _mj2
        marker_path.write_text(_mj2.dumps({"name": name, "session_id": session_id}) + "\n")
    except Exception:
        pass

    encoded_cwd = workspace.replace("/", "-")
    jsonl_path = str(Path.home() / ".claude" / "projects" / encoded_cwd / f"{session_id}.jsonl")
    lock_path = str(Path.home() / ".claude" / "projects" / encoded_cwd / f"{session_id}.lock")

    console.print(f"Starting headless worker [bold]{name}[/bold] on {machine_name}")
    console.print(f"  Server:  {effective_server_url}")
    console.print(f"  CWD:     {workspace}")
    console.print(f"  Model:   {effective_model}")
    console.print(f"  Session: {session_id}")
    console.print(f"[dim]Dashboard sends prompts here. `cfleet attach {name}` for a TUI on this session.[/dim]")

    detected_host, detected_user = _detect_ssh_target()
    effective_ssh_host = ssh_host if ssh_host is not None else detected_host
    effective_ssh_user = ssh_user if ssh_user is not None else detected_user

    try:
        asyncio.run(_agent_ws_loop(
            server_url=effective_server_url,
            token=effective_token,
            worker_name=name,
            machine_name=machine_name,
            model=effective_model,
            session_id=session_id,
            jsonl_path=jsonl_path,
            lock_path=lock_path,
            cwd=workspace,
            ssh_host=effective_ssh_host,
            ssh_user=effective_ssh_user,
            skip_permissions=skip_permissions,
        ))
    except KeyboardInterrupt:
        pass
    finally:
        try:
            Path(lock_path).unlink(missing_ok=True)
        except Exception:
            pass
        console.print(f"\n[dim]Worker {name} stopped.[/dim]")


def _run_prime_agent_worker(
    *,
    name: str,
    workspace: str,
    marker_path,  # Path to .cfleet-worker in the worker dir
    existing_session_id: Optional[str],
    model: str,
    server_url: str,
    token: str,
    machine_name: str,
    ssh_host: str,
    ssh_user: str,
) -> None:
    """Run the fleet worker loop on top of a resident prime-agent session.

    The prime session lives in the machine's prime-agent daemon and survives
    this relay process — restarts re-adopt it by worker name, so the
    conversation (and, while resident, the IPython kernel) persists.
    """
    import asyncio
    import json as _json
    from cfleet.prime_backend import (
        PrimeAgentBackend,
        PrimeBackendError,
        check_prime_available,
        run_prime_worker,
    )

    problem = check_prime_available()
    if problem:
        console.print(f"[red]prime-agent backend unavailable: {problem}[/red]")
        raise typer.Exit(1)

    # Model handling: only an explicit --model is forwarded to prime-agent;
    # otherwise the daemon's own default model is used (prime auth lives in
    # ~/.prime/agent on the worker machine, not in fleet config).
    backend = PrimeAgentBackend(name, workspace, model or None)

    console.print(f"Starting prime-agent worker [bold]{name}[/bold] on {machine_name}")
    console.print(f"  Server:  {server_url}")
    console.print(f"  CWD:     {workspace}")

    try:
        info = backend.ensure_session(resume_session_id=existing_session_id)
    except PrimeBackendError as e:
        console.print(f"[red]Failed to obtain prime-agent session: {e}[/red]")
        raise typer.Exit(1)

    console.print(f"  Session: {info.session_id} ({info.lifecycle or 'live'})")
    console.print(f"  File:    {info.session_file}")
    if info.session_id and info.session_file:
        console.print(f"[dim]Dashboard sends prompts via `prime-agent send {name}`.[/dim]")

    # Persist identity for restarts/respawns.
    try:
        marker_path.write_text(_json.dumps({
            "name": name,
            "backend": "prime",
            "session_id": info.session_id,
            "session_file": info.session_file,
        }) + "\n")
    except Exception:
        pass

    # Resolve the display model (truthful even when defaulted by the daemon).
    display_model = model or info.model
    if not display_model:
        try:
            display_model = backend.status().get("model", "") or ""
        except Exception:
            display_model = ""

    detected_host, detected_user = _detect_ssh_target()
    effective_ssh_host = ssh_host if ssh_host else detected_host
    effective_ssh_user = ssh_user if ssh_user else detected_user

    try:
        asyncio.run(run_prime_worker(
            server_url=server_url,
            token=token,
            worker_name=name,
            machine_name=machine_name,
            model=display_model,
            cwd=workspace,
            ssh_host=effective_ssh_host,
            ssh_user=effective_ssh_user,
            resume_session_id=info.session_id or None,
            info=info,
        ))
    except KeyboardInterrupt:
        pass
    finally:
        console.print(f"\n[dim]Worker {name} relay stopped (prime session persists).[/dim]")


class _AgentRuntime:
    """Per-process runtime state for `cfleet agent`."""

    def __init__(self, session_id: str, jsonl_path: str, lock_path: str, model: str, cwd: str, skip_permissions: bool = True):
        self.session_id = session_id
        self.jsonl_path = jsonl_path
        self.lock_path = lock_path
        self.model = model
        self.cwd = cwd
        self.skip_permissions = skip_permissions
        self.status: str = "idle"  # idle | working | paused
        self.current_task = None
        # If the JSONL already exists (from a prior session/restart), we must
        # use `resume` on the first SDK turn instead of `extra_args`.
        self.has_session = Path(jsonl_path).exists()
        # JSONL byte offset already pushed by the SDK streamer. The tailer
        # uses this to avoid re-pushing messages it produced.
        self.sdk_pushed_offset: int = 0


def _read_lock_owner(lock_path: str) -> Optional[str]:
    import json
    from pathlib import Path
    p = Path(lock_path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text()).get("owner")
    except Exception:
        return None


def _write_lock(lock_path: str, owner: str) -> None:
    import json
    import os
    from datetime import datetime, timezone
    from pathlib import Path
    p = Path(lock_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "owner": owner,
        "pid": os.getpid(),
        "since": datetime.now(timezone.utc).isoformat(),
    }))


def _preempt_tui_if_holding(lock_path: str, timeout_sec: float = 3.0) -> bool:
    """If the lock is held by a `cfleet attach` TUI, SIGTERM it and wait for release.

    Returns True if we preempted (or the lock was already ours/absent), False if
    we couldn't take control within the timeout. In practice we always succeed:
    the TUI's `finally:` clause releases the lock and exits promptly on SIGTERM.
    """
    import json
    import os
    import signal
    import time
    from pathlib import Path

    p = Path(lock_path)
    if not p.exists():
        return True
    try:
        cur = json.loads(p.read_text())
    except Exception:
        p.unlink(missing_ok=True)
        return True

    if cur.get("owner") != "tui":
        return True

    tui_pid = cur.get("pid")
    if not tui_pid:
        p.unlink(missing_ok=True)
        return True

    # SIGTERM the TUI. `cfleet attach` catches this via typer's default handler,
    # which invokes finally: → releases the lock → exits.
    try:
        os.kill(int(tui_pid), signal.SIGTERM)
    except ProcessLookupError:
        # TUI already gone; lock is stale — clear it.
        p.unlink(missing_ok=True)
        return True
    except Exception:
        return False

    # Wait for the TUI to clean up.
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not p.exists():
            return True
        try:
            cur = json.loads(p.read_text())
            if cur.get("owner") != "tui" or cur.get("pid") != tui_pid:
                return True
        except Exception:
            pass
        time.sleep(0.1)

    # Timed out — the TUI didn't release cleanly. Clear the lock forcibly so
    # the agent can make progress. Worst case: the TUI's finally: still runs
    # later and prints "Lock was held by another process; leaving it."
    p.unlink(missing_ok=True)
    return True


def _release_lock_if_owner(lock_path: str, expected_owner: str, expected_pid: int, new_owner: str) -> bool:
    """Flip the lock to `new_owner` only if it still matches expected owner+pid.

    Returns True if we transitioned; False if the lock was stolen or replaced
    by another process (in which case we leave it alone — they own it now).
    """
    import json
    from pathlib import Path
    p = Path(lock_path)
    if not p.exists():
        return False
    try:
        cur = json.loads(p.read_text())
    except Exception:
        return False
    if cur.get("owner") != expected_owner or cur.get("pid") != expected_pid:
        return False
    _write_lock(lock_path, new_owner)
    return True


async def _agent_ws_loop(
    server_url: str,
    token: str,
    worker_name: str,
    machine_name: str,
    model: str,
    session_id: str,
    jsonl_path: str,
    lock_path: str,
    cwd: str,
    ssh_host: str = "",
    ssh_user: str = "",
    skip_permissions: bool = True,
):
    """WebSocket client: registers, handles commands, runs SDK, tails JSONL."""
    import asyncio
    import json
    import websockets

    runtime = _AgentRuntime(session_id, jsonl_path, lock_path, model, cwd, skip_permissions=skip_permissions)
    _write_lock(lock_path, "relay")

    ws_url = server_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    backoff = 1.0

    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                await ws.send(json.dumps({
                    "type": "register",
                    "token": token,
                    "worker_name": worker_name,
                    "machine_name": machine_name,
                    "session_id": session_id,
                    "cwd": cwd,
                    "model": model,
                    "ssh_host": ssh_host,
                    "ssh_user": ssh_user,
                    "skip_permissions": skip_permissions,
                }))

                reg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if reg.get("type") != "registered":
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue

                backoff = 1.0
                tasks = [
                    asyncio.create_task(_agent_heartbeat(ws)),
                    asyncio.create_task(_agent_jsonl_tailer(ws, runtime)),
                ]

                try:
                    async for raw in ws:
                        data = json.loads(raw)
                        await _handle_server_command(ws, data, runtime, worker_name)
                finally:
                    for t in tasks:
                        t.cancel()

        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def _agent_heartbeat(ws):
    import asyncio
    import json
    while True:
        await asyncio.sleep(30)
        try:
            await ws.send(json.dumps({"type": "heartbeat"}))
        except Exception:
            break


async def _agent_jsonl_tailer(ws, runtime: "_AgentRuntime"):
    """Tail the session JSONL and push new user/assistant entries to the server.

    The SDK runner (`_agent_run_sdk`) pushes its own events live, so during an
    active SDK turn we suppress the tailer to avoid double-pushing the same
    messages. At all other times (TUI is attached, or relay is idle between
    turns) we forward any new JSONL entries — that covers TUI-authored writes
    even if the TUI detaches between our poll and the next tick.
    """
    import asyncio
    import json
    from pathlib import Path

    p = Path(runtime.jsonl_path)
    last_size = 0
    while True:
        try:
            # While the SDK is running it streams events live via _agent_run_sdk.
            # The tailer must stay silent during that window — otherwise the 0.5s
            # poll races the SDK's writes and pushes duplicates. Only tail when
            # the relay is idle (picks up TUI-authored writes after detach).
            if runtime.status == "working":
                await asyncio.sleep(0.5)
                continue

            if p.exists():
                size = p.stat().st_size
                floor = max(last_size, runtime.sdk_pushed_offset)
                if size > floor:
                    with open(p, "r") as f:
                        f.seek(floor)
                        chunk = f.read()
                    last_size = size
                    for line in chunk.splitlines():
                        if not line.strip():
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg = _jsonl_entry_to_message(entry)
                        if msg is None:
                            continue
                        try:
                            await ws.send(json.dumps({"type": "event", "data": msg}))
                        except Exception:
                            return
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(2.0)


async def _handle_server_command(ws, data: dict, runtime: "_AgentRuntime", worker_name: str):
    """Handle commands from the server: ask, messages, status, interrupt."""
    import asyncio
    import json

    msg_type = data.get("type")
    request_id = data.get("request_id", "")

    if msg_type == "heartbeat_ack":
        return

    if msg_type == "ask":
        prompt = data.get("prompt", "")

        # Dashboard-wins policy: if a `cfleet attach` TUI is holding the lock,
        # boot it so the SDK can take the turn without JSONL corruption. The
        # TUI's `finally:` block releases the lock and prints "Detached".
        # Session state persists on the JSONL; when the operator re-attaches,
        # `claude --resume` picks up including this new turn.
        _preempt_tui_if_holding(runtime.lock_path)

        if runtime.status == "working":
            await ws.send(json.dumps({
                "type": "response",
                "request_id": request_id,
                "data": {"error": "Agent is already working"},
            }))
            return

        runtime.current_task = asyncio.create_task(_agent_run_sdk(ws, runtime, prompt))
        await ws.send(json.dumps({
            "type": "response",
            "request_id": request_id,
            "data": {"ok": True, "status": "working"},
        }))
        await ws.send(json.dumps({"type": "status_update", "status": "working"}))

        async def _notify_done(task, _ws=ws, _runtime=runtime):
            try:
                await task
            except Exception:
                pass
            try:
                await _ws.send(json.dumps({"type": "status_update", "status": _runtime.status}))
            except Exception:
                pass

        asyncio.create_task(_notify_done(runtime.current_task))

    elif msg_type == "messages":
        # New: tail-first paginated read. `before` is a message index; when
        # None the reader returns the last `limit` messages efficiently
        # (reverse-tail seek). Old callers passing `offset` still work — we
        # translate them to the equivalent `before`.
        limit = int(data.get("limit", 200))
        before = data.get("before")
        if before is None and "offset" in data and data.get("offset"):
            # Legacy path: offset+limit meant "chronological forward window".
            # Preserve behavior by mapping to `before = offset + limit`.
            before = int(data["offset"]) + limit
        elif before is not None:
            before = int(before)
        result = _read_session_messages(
            runtime.jsonl_path, before=before, limit=limit,
        )
        await ws.send(json.dumps({
            "type": "response",
            "request_id": request_id,
            "data": result,
        }))

    elif msg_type == "status":
        # Cheap count-only tail read to report message_count without loading
        # the whole file into memory.
        tail = _read_session_messages(runtime.jsonl_path, before=None, limit=1)
        await ws.send(json.dumps({
            "type": "response",
            "request_id": request_id,
            "data": {
                "worker_name": worker_name,
                "model": runtime.model,
                "session_id": runtime.session_id,
                "status": runtime.status,
                "lock_owner": _read_lock_owner(runtime.lock_path),
                "message_count": tail.get("total", 0),
            },
        }))

    elif msg_type == "interrupt":
        if runtime.current_task and not runtime.current_task.done():
            runtime.current_task.cancel()
            runtime.status = "idle"
            await ws.send(json.dumps({
                "type": "response",
                "request_id": request_id,
                "data": {"ok": True, "status": "interrupted"},
            }))
            try:
                await ws.send(json.dumps({"type": "status_update", "status": "idle"}))
            except Exception:
                pass
        else:
            await ws.send(json.dumps({
                "type": "response",
                "request_id": request_id,
                "data": {"ok": True, "status": "not_running"},
            }))


async def _agent_run_sdk(ws, runtime: "_AgentRuntime", prompt: str) -> None:
    """Run a single SDK turn against the worker's session.

    Streams each SDK message to the dashboard via WebSocket events as soon as
    it arrives. The JSONL tailer is a backstop and a way to pick up
    TUI-authored turns; this is the primary live-update path for SDK turns.
    """
    import asyncio
    import json
    import sys
    import traceback
    from claude_code_sdk import query, ClaudeCodeOptions
    from cfleet.sdk_serialize import serialize_message as _serialize_message

    runtime.status = "working"

    try:
        kwargs = dict(
            model=runtime.model,
            cwd=runtime.cwd,
            permission_mode="bypassPermissions" if runtime.skip_permissions else "default",
            allowed_tools=["Read", "Write", "Edit", "MultiEdit", "Bash", "Glob", "Grep", "WebFetch"],
        )
        if runtime.has_session:
            kwargs["resume"] = runtime.session_id
        else:
            kwargs["extra_args"] = {"session-id": runtime.session_id}
        options = ClaudeCodeOptions(**kwargs)

        async for msg in query(prompt=prompt, options=options):
            serialized = _serialize_message(msg)
            role = serialized.get("role", "")
            content = serialized.get("content") or []

            if role == "assistant" and content:
                serialized["type"] = "AssistantMessage"
                try:
                    await ws.send(json.dumps({"type": "event", "data": serialized}))
                except Exception:
                    pass
            elif role == "user" and content:
                # The SDK emits UserMessage only for tool results — the original
                # user prompt comes from the dashboard's optimistic add. Skip
                # any UserMessage that doesn't contain tool results.
                has_tool_result = any(
                    isinstance(b, dict) and b.get("type") == "ToolResultBlock"
                    for b in content
                )
                if has_tool_result:
                    serialized["type"] = "UserPrompt"
                    try:
                        await ws.send(json.dumps({"type": "event", "data": serialized}))
                    except Exception:
                        pass

        runtime.has_session = True
        runtime.status = "idle"
    except asyncio.CancelledError:
        runtime.status = "idle"
        raise
    except Exception as e:
        runtime.status = "errored"
        print(f"[cfleet agent] SDK error: {e}", file=sys.stderr)
        traceback.print_exc()
    finally:
        # Move the tailer's floor past anything the SDK already pushed so we
        # don't double up when the JSONL eventually catches up to disk.
        try:
            from pathlib import Path as _P
            jp = _P(runtime.jsonl_path)
            if jp.exists():
                runtime.sdk_pushed_offset = jp.stat().st_size
        except Exception:
            pass


def _jsonl_entry_to_message(entry: dict) -> Optional[dict]:
    """Transform one raw session-JSONL entry into the dashboard's Message shape.

    Returns None for entries that aren't user/assistant turns.
    """
    entry_type = entry.get("type")
    if entry_type not in ("user", "assistant"):
        return None

    msg = entry.get("message", {})
    role = msg.get("role", entry_type)
    raw_content = msg.get("content", "")
    timestamp = entry.get("timestamp", "")

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

    return {
        "type": "UserPrompt" if role == "user" else "AssistantMessage",
        "role": role,
        "timestamp": timestamp,
        "content": content,
        "model": msg.get("model", ""),
    }


def _iter_jsonl_lines_reverse(path, chunk_size: int = 65536):
    """Yield decoded lines from `path` in reverse order without loading the file.

    Reads backwards in `chunk_size`-byte blocks; buffers partial lines across
    block boundaries. Skips empty lines. Used to fetch the tail of long session
    JSONL files efficiently (44MB session files would otherwise be fully read
    on every dashboard refresh).
    """
    with open(path, "rb") as f:
        f.seek(0, 2)  # SEEK_END
        remaining = f.tell()
        buffer = b""
        while remaining > 0:
            read = min(chunk_size, remaining)
            remaining -= read
            f.seek(remaining)
            chunk = f.read(read)
            buffer = chunk + buffer
            # Split — keep the first fragment as partial-line for the next read
            # (unless we've reached the file's start).
            lines = buffer.split(b"\n")
            if remaining > 0:
                buffer = lines[0]
                lines = lines[1:]
            else:
                buffer = b""
            # Yield in reverse so most recent line comes first.
            for line in reversed(lines):
                if line.strip():
                    yield line.decode("utf-8", errors="replace")


def _read_session_messages(
    jsonl_path: str,
    *,
    before: int | None = None,
    limit: int = 200,
) -> dict:
    """Read messages from a Claude session JSONL, tail-first with pagination.

    Args:
      before: If None, return the LAST `limit` displayable messages (efficient
        reverse-tail read). If set, return `messages[before-limit : before]` —
        the previous chunk older than the current head. `before` is the
        message-index (not byte-offset).
      limit: How many messages to return.

    Returns:
      {
        "messages":  list of message dicts in chronological order,
        "total":     total displayable messages in the JSONL,
        "head":      index of the first message returned (== before-len if
                     paging older; used by caller as next `before`),
        "has_more":  True if there are older messages to fetch (head > 0),
      }

    A "displayable message" is anything _jsonl_entry_to_message returns non-None
    for — user prompts, assistant messages, system messages we surface. The
    JSONL contains many entries we filter out (metadata, session-init, etc.),
    which is why total != file line count.
    """
    import json
    from pathlib import Path

    p = Path(jsonl_path)
    if not p.exists():
        return {"messages": [], "total": 0, "head": 0, "has_more": False}

    if before is None:
        # Tail read: walk backwards, collect the last `limit` displayable
        # messages. Also count everything we skip so we can report `total`
        # accurately. This walks the whole file but only parses lines until
        # we've filled the window — a 44MB file with 20K entries stays under
        # ~200ms because line-splitting bytes is much cheaper than JSON parsing.
        collected: list[dict] = []
        total_before = 0  # messages that come BEFORE the ones we collected
        filled = False
        for raw in _iter_jsonl_lines_reverse(p):
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg = _jsonl_entry_to_message(entry)
            if msg is None:
                continue
            if not filled:
                collected.append(msg)
                if len(collected) >= limit:
                    filled = True
            else:
                total_before += 1
        collected.reverse()
        total = total_before + len(collected)
        head = total_before
        return {
            "messages": collected,
            "total": total,
            "head": head,
            "has_more": head > 0,
        }

    # Paginated older read. `before` is a message-index. Return the window
    # [max(0, before-limit) : before]. Walk forward, count displayable
    # messages, capture the slice, early-exit past `before`.
    start = max(0, before - limit)
    end = before
    collected = []
    count = 0
    with open(p, "rb") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = _jsonl_entry_to_message(entry)
            if msg is None:
                continue
            if count >= end:
                break
            if count >= start:
                collected.append(msg)
            count += 1
    # Whether there are older messages beyond `start` — only true if start > 0.
    return {
        "messages": collected,
        "total": count if count >= end else count,  # best-effort, may be low
        "head": start,
        "has_more": start > 0,
    }


# --------------------------------------------------------------------------
# cfleet leave
# --------------------------------------------------------------------------

@app.command()
def leave():
    """Stop hosting workers and clear fleet config on this machine.

    Stops + disables the machine-agent systemd unit (if installed) and clears
    the server URL/token from ~/.cfleet/config.yml. The reverse of `cfleet join`.
    """
    import subprocess
    from cfleet.config import FleetConfig

    try:
        cfg = FleetConfig.load()
    except FileNotFoundError:
        console.print("[red]Not configured. Nothing to leave.[/red]")
        raise typer.Exit(1)

    if _has_systemd():
        sudo = [] if os.geteuid() == 0 else ["sudo"]
        try:
            subprocess.run(
                [*sudo, "systemctl", "disable", "--now", "cfleet-machine-agent.service"],
                capture_output=True,
                check=False,
            )
            subprocess.run(
                [*sudo, "rm", "-f", "/etc/systemd/system/cfleet-machine-agent.service"],
                capture_output=True,
                check=False,
            )
            subprocess.run([*sudo, "systemctl", "daemon-reload"], capture_output=True, check=False)
            console.print("[dim]Stopped cfleet-machine-agent.service.[/dim]")
        except Exception:
            pass

    old_url = cfg.server.url
    cfg.server.url = ""
    cfg.server.token = ""
    cfg.save()

    if old_url:
        console.print(f"[green]Left fleet at {old_url}. Server URL and token cleared.[/green]")
    else:
        console.print("[dim]No fleet connection configured.[/dim]")


# --------------------------------------------------------------------------
# cfleet attach
# --------------------------------------------------------------------------

@app.command()
def attach(
    name: str = typer.Argument(..., help="Worker name"),
    local: bool = typer.Option(False, "--local", help="Force local exec (skip SSH); used internally when SSH'd in"),
    skip_permissions: Optional[bool] = typer.Option(None, "--skip-permissions/--no-skip-permissions", help="Override worker's permission mode for this attach session"),
):
    """Drop into a `claude` TUI on the worker's session.

    Pauses the headless relay so the TUI is the sole writer. When you exit the
    TUI (Ctrl-D or :q), the relay resumes accepting dashboard prompts.

    Run from anywhere — fetches SSH info from the server and connects to the
    worker's host. Use `--local` if you're already on the right machine.
    """
    import platform
    import subprocess
    import urllib.error
    import urllib.request
    import json as _json
    from pathlib import Path
    from cfleet.config import FleetConfig

    try:
        cfg = FleetConfig.load()
    except FileNotFoundError:
        console.print("[red]Run 'cfleet init' or 'cfleet join' first.[/red]")
        raise typer.Exit(1)

    server_url = cfg.server.url
    token = cfg.server.token
    if not server_url or not token:
        console.print("[red]No server configured. Run 'cfleet join <url>' first.[/red]")
        raise typer.Exit(1)

    req = urllib.request.Request(
        f"{server_url.rstrip('/')}/api/workers/{name}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            worker = _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        console.print(f"[red]Server returned {e.code}: {e.reason}[/red]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Could not reach server: {e}[/red]")
        raise typer.Exit(1)

    if not worker.get("connected"):
        console.print(f"[red]Worker '{name}' is not connected to the server.[/red]")
        raise typer.Exit(1)

    session_id = worker.get("session_id")
    if not session_id:
        console.print(f"[red]Worker '{name}' has no session yet (send a prompt first to initialize it).[/red]")
        raise typer.Exit(1)

    workspace = worker.get("cwd", "")
    if not workspace:
        console.print(f"[red]Worker '{name}' has no cwd recorded.[/red]")
        raise typer.Exit(1)

    worker_machine = worker.get("machine_name", "")
    this_machine = platform.node()

    if not local and worker_machine and worker_machine != this_machine:
        ssh_host = worker.get("ssh_host", "")
        ssh_user = worker.get("ssh_user", "")
        if not ssh_host:
            console.print(
                f"[red]Worker '{name}' is on '{worker_machine}', which has no SSH host registered.[/red]\n"
                f"[dim]Run `cfleet attach {name}` directly on that machine, "
                "or restart its `cfleet agent` with --ssh-host.[/dim]"
            )
            raise typer.Exit(1)
        import re
        # ssh concatenates the remote argv into a shell command on the other
        # side, so the worker name reaches a remote shell. Lock to a strict
        # character set to keep that hop injection-free.
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            console.print(f"[red]Worker name '{name}' contains unsupported characters.[/red]")
            raise typer.Exit(1)
        target = f"{ssh_user}@{ssh_host}" if ssh_user else ssh_host
        console.print(f"[dim]SSHing to {target} and attaching...[/dim]")
        skip_flag = ""
        if skip_permissions is True:
            skip_flag = " --skip-permissions"
        elif skip_permissions is False:
            skip_flag = " --no-skip-permissions"
        remote_cmd = f"bash -lc 'cfleet attach {name} --local{skip_flag}'"
        rc = subprocess.run(["ssh", "-t", target, remote_cmd]).returncode
        raise typer.Exit(rc)

    # Local exec path — prime-agent workers attach natively via the daemon.
    # The server record is the source of truth for the backend, but servers
    # running pre-prime code don't report it — fall back to the on-disk worker
    # marker (.cfleet-worker), which every prime relay writes.
    attach_backend = worker.get("agent_backend") or ""
    if attach_backend not in ("claude", "prime"):
        try:
            _marker = _json.loads((Path(workspace) / ".cfleet-worker").read_text())
            attach_backend = _marker.get("backend", "") or "claude"
        except Exception:
            attach_backend = "claude"

    if attach_backend == "prime":
        import subprocess as _sp
        import shutil as _shutil_pa
        from cfleet.prime_backend import PrimeAgentBackend, check_prime_available

        prime_bin = _shutil_pa.which("prime-agent")
        if not prime_bin:
            # Non-interactive SSH gives a minimal PATH; check common install spots.
            home = os.environ.get("HOME", "")
            for candidate in (
                f"{home}/.npm-global/bin/prime-agent",
                f"{home}/.local/bin/prime-agent",
                "/usr/local/bin/prime-agent",
            ):
                if Path(candidate).exists():
                    prime_bin = candidate
                    break
        if not prime_bin:
            console.print("[red]`prime-agent` CLI not found on PATH or in common install dirs.[/red]")
            raise typer.Exit(1)

        problem = check_prime_available()
        if problem:
            console.print(f"[red]prime-agent unavailable here: {problem}[/red]")
            raise typer.Exit(1)
        backend = PrimeAgentBackend(name, workspace)
        try:
            console.print(f"[dim]Waking prime session for {name} (if needed)...[/dim]")
            backend.wake()
        except Exception as e:
            console.print(f"[yellow]Could not wake session ({e}); trying attach anyway.[/yellow]")
        console.print(f"[dim]Attaching to prime-agent session '{name}' — detaching leaves it running.[/dim]")
        rc = _sp.run([prime_bin, "attach", name], cwd=workspace).returncode
        raise typer.Exit(rc)

    model = worker.get("model") or cfg.resolve_model() or "claude-opus-4-6"
    env = os.environ.copy()
    from cfleet.config import resolve_provider_env as _resolve_prov_attach
    _prov_env = _resolve_prov_attach(model, cfg)
    if _prov_env:
        env.update(_prov_env)
    else:
        try:
            _auth_mode = (Path.home() / ".cfleet" / "auth-mode").read_text().strip()
        except FileNotFoundError:
            _auth_mode = ""
        if _auth_mode == "oauth":
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("CLAUDE_CODE_API_KEY", None)
        else:
            api_key = _resolve_anthropic_key_fresh(cfg)
            if api_key:
                env["ANTHROPIC_API_KEY"] = api_key

    # Wire the broker env + git credential helper for `claude`'s subprocesses,
    # mirroring what `cfleet agent` does at startup. Without this, `git clone`
    # from the TUI hits the user's gitconfig (often `gh auth git-credential`)
    # and fails, even though the headless agent's clones would have worked.
    env["CFLEET_SERVER_URL"] = server_url
    env["CFLEET_TOKEN"] = token
    env["CFLEET_WORKER_NAME"] = name
    import shutil as _shutil_attach
    gh_helper_attach = _shutil_attach.which("cfleet-gh-token")
    if gh_helper_attach:
        existing_count = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
        env["GIT_CONFIG_COUNT"] = str(existing_count + 3)
        env[f"GIT_CONFIG_KEY_{existing_count}"] = "credential.https://github.com.helper"
        env[f"GIT_CONFIG_VALUE_{existing_count}"] = ""
        env[f"GIT_CONFIG_KEY_{existing_count + 1}"] = "credential.https://github.com.helper"
        env[f"GIT_CONFIG_VALUE_{existing_count + 1}"] = gh_helper_attach
        env[f"GIT_CONFIG_KEY_{existing_count + 2}"] = "credential.https://github.com.useHttpPath"
        env[f"GIT_CONFIG_VALUE_{existing_count + 2}"] = "true"

        # `gh` CLI shim — mint a fresh token per invocation so `gh repo list`,
        # `gh pr create`, etc. work without `gh auth login`. Lives next to the
        # workspace dir so it's tidy and gone when the worker is cleaned up.
        real_gh_attach = _shutil_attach.which("gh")
        if real_gh_attach:
            shim_dir = Path(workspace).parent / ".cfleet-bin"
            shim_dir.mkdir(parents=True, exist_ok=True)
            shim_path = shim_dir / "gh"
            shim_path.write_text(
                "#!/usr/bin/env bash\n"
                f'exec env GH_TOKEN="$(printf \'host=github.com\\n\\n\' | {gh_helper_attach} get '
                "| awk -F= '/^password=/{print $2}')\" "
                f'{real_gh_attach} "$@"\n'
            )
            shim_path.chmod(0o755)
            env["PATH"] = f"{shim_dir}:{env.get('PATH', '')}"

    encoded_cwd = workspace.replace("/", "-")
    lock_path = Path.home() / ".claude" / "projects" / encoded_cwd / f"{session_id}.lock"
    if not Path(workspace).exists():
        console.print(f"[red]Working directory does not exist locally: {workspace}[/red]")
        raise typer.Exit(1)

    import shutil as _shutil
    claude_bin = _shutil.which("claude")
    if not claude_bin:
        # Non-interactive SSH gives us a minimal PATH; check common install spots.
        home = os.environ.get("HOME", "")
        for candidate in (
            f"{home}/.local/bin/claude",
            f"{home}/.npm-global/bin/claude",
            "/usr/local/bin/claude",
            "/usr/bin/claude",
        ):
            if candidate and Path(candidate).exists():
                claude_bin = candidate
                break
    if not claude_bin:
        console.print("[red]`claude` CLI not found on PATH or in common install dirs.[/red]")
        raise typer.Exit(1)

    # Use the attach-time override if given; otherwise fall back to the
    # permission mode the worker registered with (default True).
    effective_skip = skip_permissions if skip_permissions is not None else bool(worker.get("skip_permissions", True))

    _write_lock(str(lock_path), "tui")
    our_pid = os.getpid()

    import shutil as _shutil_screen
    screen_bin = _shutil_screen.which("screen")
    screen_name = f"cfleet-{name}"

    claude_cmd_str = claude_bin + " --resume " + session_id + " --model " + model
    if effective_skip:
        claude_cmd_str += " --dangerously-skip-permissions"

    if screen_bin:
        # Check if a screen session for this worker already exists (previous
        # attach survived an SSH drop). If so, just reattach to it.
        existing = subprocess.run(
            [screen_bin, "-ls", screen_name],
            capture_output=True, text=True,
        )
        if screen_name in existing.stdout:
            console.print(f"[dim]Reconnecting to existing session for {name}...[/dim]")
            try:
                subprocess.run([screen_bin, "-x", screen_name], cwd=workspace, env=env)
            finally:
                if _release_lock_if_owner(str(lock_path), "tui", our_pid, "relay"):
                    console.print("[dim]Detached. Dashboard control restored.[/dim]")
        else:
            console.print(f"[dim]Lock acquired. Launching claude --resume in screen session '{screen_name}'...[/dim]")
            # Write env vars to a temp file so screen inherits them — screen
            # doesn't forward the parent's env to the child shell.
            env_script = Path(workspace) / ".cfleet-attach-env.sh"
            try:
                lines = ["#!/bin/bash"]
                for k, v in env.items():
                    if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CFLEET_", "GIT_CONFIG_", "GH_", "PATH")):
                        lines.append(f"export {k}={__import__('shlex').quote(v)}")
                lines.append(f"cd {__import__('shlex').quote(workspace)}")
                lines.append(f"exec {claude_cmd_str}")
                env_script.write_text("\n".join(lines) + "\n")
                env_script.chmod(0o700)
                subprocess.run(
                    [screen_bin, "-S", screen_name, "-t", name, str(env_script)],
                    cwd=workspace, env=env,
                )
            finally:
                env_script.unlink(missing_ok=True)
                if _release_lock_if_owner(str(lock_path), "tui", our_pid, "relay"):
                    console.print("[dim]Detached. Dashboard control restored.[/dim]")
                else:
                    console.print("[dim yellow]Detached. Dashboard took over (or another process holds the lock).[/dim yellow]")
    else:
        console.print(f"[dim]Lock acquired. Launching claude --resume on session {session_id[:8]}...[/dim]")
        console.print("[dim yellow]screen not found — session will not survive SSH disconnects. Install screen for persistence.[/dim yellow]")
        claude_cmd = [claude_bin, "--resume", session_id, "--model", model]
        if effective_skip:
            claude_cmd.append("--dangerously-skip-permissions")
        try:
            subprocess.run(claude_cmd, cwd=workspace, env=env)
        finally:
            if _release_lock_if_owner(str(lock_path), "tui", our_pid, "relay"):
                console.print("[dim]Detached. Dashboard control restored.[/dim]")
            else:
                console.print("[dim yellow]Detached. Dashboard took over (or another process holds the lock).[/dim yellow]")


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
    """Set a worker's GitHub access level. Takes effect on next token renewal.

    For local (`cfleet agent`) workers on this machine, also wires up the git
    credential helper in the worker's cwd so `git clone/push` Just Works.
    """
    from cfleet.config import FleetState, GitHubLevel

    _validate_enum(level, GitHubLevel, "level")

    if _use_remote_server():
        result = _api_request("PUT", f"/api/github/level/{worker_name}", body={"level": level})
        console.print(
            f"[green]Set {worker_name} -> {result.get('github_level', level)}[/green]"
            f"  [dim](was: {result.get('previous_level', '?')})[/dim]"
        )
        for w in result.get("branch_protection_warnings", []) or []:
            console.print(f"[yellow]  ⚠ {w}[/yellow]")
        return

    state = FleetState.load()
    if worker_name not in state.workers:
        console.print(f"[red]Worker '{worker_name}' not found.[/red]")
        raise typer.Exit(1)

    _set_worker_gh_level(worker_name, level)

    worker = state.workers[worker_name]
    if worker.local_mode and worker.cwd and level != "none":
        _wire_local_git_credential_helper(worker_name, worker.cwd)


# --------------------------------------------------------------------------
# cfleet gh get
# --------------------------------------------------------------------------

@gh_app.command("get")
def gh_get(
    worker_name: str = typer.Argument(..., help="Worker name"),
):
    """Show a worker's current GitHub access level."""
    from cfleet.config import FleetState, GH_PERMISSION_MAP, GitHubLevel

    if _use_remote_server():
        data = _api_request("GET", f"/api/github/level/{worker_name}")
        level_str = data.get("github_level", "none")
        level = GitHubLevel(level_str)
        perms = GH_PERMISSION_MAP.get(level, {})

        # Fetch repos from /api/workers/{name} (the level endpoint doesn't include them).
        try:
            worker = _api_request("GET", f"/api/workers/{worker_name}")
            repos = worker.get("repos", []) or []
        except SystemExit:
            repos = []

        console.print(f"\n[bold]{worker_name}[/bold]")
        console.print(f"  GitHub level: [bold]{level.value}[/bold]")
        if perms:
            perm_str = ", ".join(f"{k}:{v}" for k, v in perms.items())
            console.print(f"  Permissions:  {perm_str}")
        if repos:
            console.print(f"  Repos:        {', '.join(repos)}")
        else:
            console.print("  Repos:        [dim]all installed repos[/dim]")
        console.print()
        return

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

    if _use_remote_server():
        params = []
        if worker_name:
            params.append(f"worker_name={worker_name}")
        params.append(f"limit={limit}")
        rows = _api_request("GET", f"/api/github/log?{'&'.join(params)}")
        entries = [
            type("E", (), {
                "timestamp": e.get("timestamp", ""),
                "worker_name": e.get("worker_name", ""),
                "level": e.get("level", ""),
                "repos": e.get("repos", []) or [],
                "expires_at": e.get("expires_at", ""),
            })()
            for e in (rows or [])
        ]
    else:
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


# --------------------------------------------------------------------------
# cfleet auth — toggle host between Anthropic API-key and Claude account auth
# --------------------------------------------------------------------------

_AUTH_MODE_PATH = Path.home() / ".cfleet" / "auth-mode"
_API_KEY_FILE = Path.home() / ".claude" / ".api-key"
_CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"
_BASHRC_MARKER_BEGIN = "# >>> cfleet auth (managed) >>>"
_BASHRC_MARKER_END = "# <<< cfleet auth (managed) <<<"


def _read_auth_mode() -> str:
    """Returns 'api', 'oauth', or '' (unset). Machine-agent reads this too."""
    try:
        return _AUTH_MODE_PATH.read_text().strip()
    except FileNotFoundError:
        return ""


def _write_auth_mode(mode: str) -> None:
    _AUTH_MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _AUTH_MODE_PATH.write_text(mode + "\n")


def _replace_bashrc_block(new_block: str) -> None:
    """Idempotently write a managed cfleet-auth block into ~/.bashrc.

    Removes any prior block, appends the new one. Empty new_block just removes
    the prior block (used by `cfleet auth oauth`).
    """
    bashrc = Path.home() / ".bashrc"
    existing = ""
    if bashrc.exists():
        existing = bashrc.read_text()

    lines = existing.splitlines()
    kept: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == _BASHRC_MARKER_BEGIN:
            inside = True
            continue
        if line.strip() == _BASHRC_MARKER_END:
            inside = False
            continue
        if not inside:
            kept.append(line)

    out = "\n".join(kept).rstrip() + "\n"
    if new_block:
        out += f"\n{_BASHRC_MARKER_BEGIN}\n{new_block.rstrip()}\n{_BASHRC_MARKER_END}\n"

    bashrc.write_text(out)


@auth_app.command("api")
def auth_api():
    """Switch this host to Anthropic API-key auth for both shell and workers."""
    from cfleet.config import FleetConfig

    try:
        cfg = FleetConfig.load()
    except FileNotFoundError:
        console.print("[red]Run 'cfleet init' or 'cfleet join' first.[/red]")
        raise typer.Exit(1)

    key = _resolve_anthropic_key_fresh(cfg)
    if not key:
        console.print("[red]No Anthropic API key available (server has none, and no local cache).[/red]")
        console.print("[dim]Set one with: cfleet secret set anthropic sk-ant-...[/dim]")
        raise typer.Exit(1)

    _API_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _API_KEY_FILE.write_text(key)
    _API_KEY_FILE.chmod(0o600)

    _replace_bashrc_block(
        f'export ANTHROPIC_API_KEY="$(cat {_API_KEY_FILE})"\n'
        f'export CLAUDE_CODE_API_KEY="$ANTHROPIC_API_KEY"'
    )
    _write_auth_mode("api")

    console.print(f"[green]Auth mode: api[/green]")
    console.print(f"  Wrote key to {_API_KEY_FILE}")
    console.print(f"  Bashrc export block updated (`source ~/.bashrc` in existing shells).")
    console.print(f"  Newly spawned workers on this host will use ANTHROPIC_API_KEY.")


@auth_app.command("oauth")
def auth_oauth():
    """Switch this host to Claude account (OAuth) auth for both shell and workers.

    Removes the API key from shell env + machine-agent injection. `claude` will
    fall through to ~/.claude/.credentials.json — run `claude` interactively once
    to complete the OAuth flow if you haven't already.
    """
    _API_KEY_FILE.unlink(missing_ok=True)
    _replace_bashrc_block("")
    _write_auth_mode("oauth")

    console.print(f"[green]Auth mode: oauth[/green]")
    console.print(f"  Removed {_API_KEY_FILE}")
    console.print(f"  Cleared managed bashrc export block (`source ~/.bashrc` in existing shells).")
    if _CLAUDE_CREDS.exists():
        console.print(f"  Existing OAuth credentials found at {_CLAUDE_CREDS} — you're set.")
    else:
        console.print(f"[yellow]  No OAuth credentials yet at {_CLAUDE_CREDS}.[/yellow]")
        console.print(f"[yellow]  Run `claude` interactively once to complete login.[/yellow]")
    console.print(f"  Newly spawned workers on this host will inherit whatever `claude` finds — env is scrubbed.")


@auth_app.command("status")
def auth_status():
    """Show current auth mode + presence of API key / OAuth credentials."""
    mode = _read_auth_mode() or "(unset — defaults to api if ANTHROPIC_API_KEY is set)"
    api_key_present = _API_KEY_FILE.exists()
    oauth_present = _CLAUDE_CREDS.exists()
    env_key_set = bool(os.environ.get("ANTHROPIC_API_KEY"))

    console.print(f"[bold]Auth mode:[/bold] {mode}")
    console.print(f"  ~/.claude/.api-key          {'✓ present' if api_key_present else '✗ absent'}")
    console.print(f"  ~/.claude/.credentials.json {'✓ present' if oauth_present else '✗ absent'}")
    console.print(f"  ANTHROPIC_API_KEY in env    {'✓ set (this shell)' if env_key_set else '✗ unset (this shell)'}")
