"""Textual TUI app — three-panel layout for managing fleet workers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from rich.text import Text
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from open_fleet.config import FleetConfig, FleetState, WorkerState


# ---------------------------------------------------------------------------
# Spawn dialog
# ---------------------------------------------------------------------------


class SpawnDialog(ModalScreen[dict | None]):
    """Modal dialog for spawning a new worker."""

    CSS = """
    SpawnDialog {
        align: center middle;
    }
    #spawn-dialog {
        width: 60;
        height: auto;
        max-height: 20;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #spawn-dialog Label {
        margin-bottom: 1;
    }
    #spawn-dialog Input {
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="spawn-dialog"):
            yield Label("Spawn New Worker", classes="title")
            yield Label("Name:")
            yield Input(id="spawn-name", placeholder="my-worker")
            yield Label("Model (blank = default):")
            yield Input(id="spawn-model", placeholder="claude-sonnet-4-5-20250514")
            yield Label("Instance type (blank = default):")
            yield Input(id="spawn-instance", placeholder="Standard_D2s_v5")
            yield Label("[Enter] spawn  [Escape] cancel")

    @on(Input.Submitted, "#spawn-name")
    def submit_name(self, event: Input.Submitted) -> None:
        self.query_one("#spawn-model", Input).focus()

    @on(Input.Submitted, "#spawn-model")
    def submit_model(self, event: Input.Submitted) -> None:
        self.query_one("#spawn-instance", Input).focus()

    @on(Input.Submitted, "#spawn-instance")
    def submit_instance(self, event: Input.Submitted) -> None:
        name = self.query_one("#spawn-name", Input).value.strip()
        if not name:
            self.notify("Name is required", severity="error")
            return
        model = self.query_one("#spawn-model", Input).value.strip() or None
        instance = self.query_one("#spawn-instance", Input).value.strip() or None
        self.dismiss({"name": name, "model": model, "instance_type": instance})

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Confirm dialog
# ---------------------------------------------------------------------------


class ConfirmDialog(ModalScreen[bool]):
    """Simple yes/no confirmation dialog."""

    CSS = """
    ConfirmDialog {
        align: center middle;
    }
    #confirm-dialog {
        width: 50;
        height: auto;
        max-height: 10;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
        Binding("escape", "no", "Cancel"),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self._message)
            yield Label("[y]es  [n]o")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Path input dialog
# ---------------------------------------------------------------------------


class PathDialog(ModalScreen[str | None]):
    """Dialog for entering a file path."""

    CSS = """
    PathDialog {
        align: center middle;
    }
    #path-dialog {
        width: 60;
        height: auto;
        max-height: 10;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, label: str, placeholder: str = "") -> None:
        super().__init__()
        self._label = label
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="path-dialog"):
            yield Label(self._label)
            yield Input(id="path-input", placeholder=self._placeholder)
            yield Label("[Enter] confirm  [Escape] cancel")

    @on(Input.Submitted, "#path-input")
    def submit_path(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value if value else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Main TUI app
# ---------------------------------------------------------------------------


class FleetTUI(App):
    """Three-panel TUI for managing fleet workers."""

    TITLE = "Open Fleet"

    CSS = """
    #main-layout {
        height: 1fr;
    }
    #left-panel {
        width: 30;
        border-right: solid $primary;
        padding: 1;
    }
    #right-panel {
        width: 1fr;
        padding: 1;
    }
    #worker-detail {
        height: auto;
        max-height: 12;
        margin-bottom: 1;
    }
    #log-panel {
        height: 1fr;
        border: solid $primary;
        overflow-y: auto;
    }
    #prompt-input {
        dock: bottom;
        margin-top: 1;
    }
    .worker-idle {
        color: green;
    }
    .worker-working {
        color: yellow;
    }
    .worker-errored {
        color: red;
    }
    .worker-stopped {
        color: $text-muted;
    }
    .worker-spawning, .worker-provisioning {
        color: cyan;
    }
    """

    BINDINGS = [
        Binding("s", "spawn", "Spawn", show=True),
        Binding("k", "kill_worker", "Kill", show=True),
        Binding("a", "attach_worker", "Attach", show=True),
        Binding("f", "send_files", "Send", show=True),
        Binding("c", "collect_files", "Collect", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._config: FleetConfig | None = None
        self._state = FleetState()
        self._selected_worker: str | None = None
        self._log_streaming = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-layout"):
            with Vertical(id="left-panel"):
                yield Label("Workers", classes="title")
                yield ListView(id="worker-list")
            with Vertical(id="right-panel"):
                yield Static(id="worker-detail", markup=True)
                with VerticalScroll(id="log-panel"):
                    yield Static(id="log-content", markup=True)
                yield Input(id="prompt-input", placeholder="Send prompt to selected worker...")
        yield Footer()

    def on_mount(self) -> None:
        try:
            self._config = FleetConfig.load()
        except FileNotFoundError:
            self._config = FleetConfig()
        self._refresh_workers()
        self.set_interval(5.0, self._refresh_workers)

    # ------------------------------------------------------------------
    # Worker list management
    # ------------------------------------------------------------------

    def _refresh_workers(self) -> None:
        """Reload state.json and update the worker list.

        Only rebuilds the ListView when the set of workers or their statuses
        have actually changed, to avoid clearing/re-appending on every tick
        (which kills the log stream and causes a flash).
        """
        self._state = FleetState.load()

        # Build a snapshot of current workers to compare
        new_snapshot = {
            name: worker.status for name, worker in self._state.workers.items()
        }
        if new_snapshot == getattr(self, "_worker_snapshot", None):
            # Nothing changed — skip rebuild, just update detail if selected
            if self._selected_worker:
                self._update_detail(self._selected_worker)
            return
        self._worker_snapshot = new_snapshot

        listview = self.query_one("#worker-list", ListView)
        old_selection = self._selected_worker

        listview.clear()
        for name, worker in self._state.workers.items():
            status_icon = {
                "idle": "[green]●[/green]",
                "working": "[yellow]●[/yellow]",
                "errored": "[red]●[/red]",
                "stopped": "[dim]○[/dim]",
                "spawning": "[cyan]◐[/cyan]",
                "provisioning": "[cyan]◑[/cyan]",
            }.get(worker.status, "○")
            item = ListItem(Label(f"{status_icon} {name}  {worker.status}", markup=True))
            item._worker_name = name  # stash for lookup
            listview.append(item)

        # Restore selection
        if old_selection:
            for i, item in enumerate(listview.children):
                if getattr(item, "_worker_name", None) == old_selection:
                    listview.index = i
                    break

    def _get_selected_worker_name(self) -> str | None:
        listview = self.query_one("#worker-list", ListView)
        if listview.highlighted_child is not None:
            return getattr(listview.highlighted_child, "_worker_name", None)
        return None

    @on(ListView.Highlighted, "#worker-list")
    def worker_selected(self, event: ListView.Highlighted) -> None:
        if event.item is None:
            return
        name = getattr(event.item, "_worker_name", None)
        if name:
            self._selected_worker = name
            self._update_detail(name)
            self._start_log_stream(name)

    def _update_detail(self, name: str) -> None:
        """Update the detail panel for a worker."""
        worker = self._state.workers.get(name)
        if not worker:
            self.query_one("#worker-detail", Static).update("")
            return

        prompt_display = ""
        if worker.last_prompt:
            truncated = worker.last_prompt[:60] + "..." if len(worker.last_prompt) > 60 else worker.last_prompt
            prompt_display = f"\n  Last: \"{truncated}\""

        detail = (
            f"[bold]{worker.name}[/bold]\n"
            f"  IP:     {worker.ip or '—'}\n"
            f"  Model:  {worker.model}\n"
            f"  Repos:  {', '.join(worker.repos) if worker.repos else '—'}\n"
            f"  Status: {worker.status}"
            f"{prompt_display}"
        )
        self.query_one("#worker-detail", Static).update(detail)

    # ------------------------------------------------------------------
    # Log streaming
    # ------------------------------------------------------------------

    @work(exclusive=True, group="log_stream")
    async def _start_log_stream(self, name: str) -> None:
        """Stream tmux logs for the selected worker. Polls every 3 seconds.

        Uses Static.update() with rich Text for atomic, flash-free updates.
        """
        log_widget = self.query_one("#log-content", Static)
        log_scroll = self.query_one("#log-panel", VerticalScroll)

        worker = self._state.workers.get(name)
        if not worker or not worker.ip or worker.status in ("stopped", "spawning"):
            log_widget.update("[dim]No logs available[/dim]")
            return

        from open_fleet.ssh import WorkerSSH

        ssh = WorkerSSH(
            ip=worker.ip,
            user=self._config.cloud.ssh_user if self._config else "azureuser",
            key_path=str(self._config.resolve_ssh_key()) if self._config else "~/.ssh/id_ed25519",
        )

        seen_content = ""
        while self._selected_worker == name:
            try:
                output = await asyncio.to_thread(ssh.read_logs, 80)
                if output != seen_content:
                    seen_content = output
                    # Use Text() so raw terminal output isn't parsed as Rich markup
                    log_widget.update(Text(output.rstrip()))
                    log_scroll.scroll_end(animate=False)

                # Check if Claude Code returned to idle
                self._state = FleetState.load()
                w = self._state.workers.get(name)
                if w and w.status == "working":
                    is_idle = await asyncio.to_thread(ssh.is_claude_idle)
                    if is_idle:
                        w.status = "idle"
                        self._state.save()
                        self._worker_snapshot = None  # force list rebuild
                        self._refresh_workers()
            except Exception as e:
                log_widget.update(f"[red]Connection error: {e}[/red]")
            await asyncio.sleep(3.0)

        ssh.close()

    # ------------------------------------------------------------------
    # Prompt input
    # ------------------------------------------------------------------

    @on(Input.Submitted, "#prompt-input")
    def send_prompt(self, event: Input.Submitted) -> None:
        """Send prompt to the selected worker."""
        name = self._selected_worker
        prompt_text = event.value.strip()
        if not name or not prompt_text:
            return

        event.input.value = ""
        self._do_send_prompt(name, prompt_text)

    @work(thread=True)
    def _do_send_prompt(self, name: str, prompt: str) -> None:
        from open_fleet.engine import FleetEngine

        try:
            engine = FleetEngine(config=self._config)
            engine.ask(name, prompt)
            self.call_from_thread(self.notify, f"Prompt sent to {name}")
            self.call_from_thread(self._refresh_workers)
        except Exception as e:
            self.call_from_thread(self.notify, f"Error: {e}", severity="error")

    # ------------------------------------------------------------------
    # Key bindings
    # ------------------------------------------------------------------

    def action_spawn(self) -> None:
        """Open spawn dialog."""
        def on_result(result: dict | None) -> None:
            if result:
                self._do_spawn(result)

        self.push_screen(SpawnDialog(), callback=on_result)

    @work(thread=True)
    def _do_spawn(self, params: dict) -> None:
        from open_fleet.engine import FleetEngine

        try:
            engine = FleetEngine(config=self._config)
            engine.spawn(
                name=params["name"],
                model=params.get("model"),
                instance_type=params.get("instance_type"),
            )
            self.call_from_thread(self.notify, f"Worker {params['name']} spawned")
            self.call_from_thread(self._refresh_workers)
        except Exception as e:
            self.call_from_thread(self.notify, f"Spawn failed: {e}", severity="error")

    def action_kill_worker(self) -> None:
        """Kill the selected worker."""
        name = self._get_selected_worker_name()
        if not name:
            self.notify("No worker selected", severity="warning")
            return

        def on_confirm(confirmed: bool) -> None:
            if confirmed:
                self._do_kill(name)

        self.push_screen(ConfirmDialog(f"Kill worker '{name}'?"), callback=on_confirm)

    @work(thread=True)
    def _do_kill(self, name: str) -> None:
        from open_fleet.engine import FleetEngine

        try:
            engine = FleetEngine(config=self._config)
            engine.kill(name, force=True)
            self.call_from_thread(self.notify, f"Worker {name} destroyed")
            self.call_from_thread(self._refresh_workers)
        except Exception as e:
            self.call_from_thread(self.notify, f"Kill failed: {e}", severity="error")

    def action_attach_worker(self) -> None:
        """Attach to the selected worker's tmux session."""
        name = self._get_selected_worker_name()
        if not name:
            self.notify("No worker selected", severity="warning")
            return

        worker = self._state.workers.get(name)
        if not worker or not worker.ip:
            self.notify("Worker has no IP", severity="warning")
            return

        # Suspend the TUI, attach via SSH, resume on detach
        from open_fleet.ssh import WorkerSSH

        ssh_key = str(self._config.resolve_ssh_key()) if self._config else "~/.ssh/id_ed25519"
        ssh_user = self._config.cloud.ssh_user if self._config else "azureuser"

        with self.suspend():
            import subprocess
            subprocess.run([
                "ssh", "-t",
                "-i", ssh_key,
                "-o", "StrictHostKeyChecking=no",
                f"{ssh_user}@{worker.ip}",
                "tmux", "attach", "-t", "claude",
            ])

    def action_send_files(self) -> None:
        """Send files to the selected worker."""
        name = self._get_selected_worker_name()
        if not name:
            self.notify("No worker selected", severity="warning")
            return

        def on_path(path: str | None) -> None:
            if path:
                self._do_send_files(name, path)

        self.push_screen(PathDialog("Local path to send:", "/path/to/files"), callback=on_path)

    @work(thread=True)
    def _do_send_files(self, name: str, local_path: str) -> None:
        from open_fleet.engine import FleetEngine

        try:
            engine = FleetEngine(config=self._config)
            engine.send(name, local_path)
            self.call_from_thread(self.notify, f"Files sent to {name}")
        except Exception as e:
            self.call_from_thread(self.notify, f"Send failed: {e}", severity="error")

    def action_collect_files(self) -> None:
        """Collect files from the selected worker."""
        name = self._get_selected_worker_name()
        if not name:
            self.notify("No worker selected", severity="warning")
            return

        def on_path(path: str | None) -> None:
            if path:
                self._do_collect_files(name, path)

        self.push_screen(PathDialog("Local destination path:", "./collected"), callback=on_path)

    @work(thread=True)
    def _do_collect_files(self, name: str, local_dest: str) -> None:
        from open_fleet.engine import FleetEngine

        try:
            engine = FleetEngine(config=self._config)
            engine.collect(name, local_dest)
            self.call_from_thread(self.notify, f"Files collected from {name}")
        except Exception as e:
            self.call_from_thread(self.notify, f"Collect failed: {e}", severity="error")
