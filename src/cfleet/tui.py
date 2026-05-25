"""Textual TUI app — three-panel layout for managing fleet workers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from cfleet.config import FleetConfig, FleetState, WorkerState


class WorkerListItem(ListItem):
    def __init__(self, *args, worker_name: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.worker_name = worker_name


# ---------------------------------------------------------------------------
# Spawn dialog
# ---------------------------------------------------------------------------


class SpawnDialog(ModalScreen[dict | None]):
    CSS = """
    SpawnDialog { align: center middle; }
    #spawn-dialog {
        width: 60; height: auto; max-height: 24;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    #spawn-dialog Label { margin-bottom: 1; }
    #spawn-dialog Input { margin-bottom: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="spawn-dialog"):
            yield Label("Spawn New Worker", classes="title")
            yield Label("Name:")
            yield Input(id="spawn-name", placeholder="my-worker")
            yield Label("Machine (blank = auto-create):")
            yield Input(id="spawn-machine", placeholder="machine-name")
            yield Label("Model (blank = default):")
            yield Input(id="spawn-model", placeholder="claude-opus-4-6")
            yield Label("[Enter] spawn  [Escape] cancel")

    @on(Input.Submitted, "#spawn-name")
    def submit_name(self, event: Input.Submitted) -> None:
        self.query_one("#spawn-machine", Input).focus()

    @on(Input.Submitted, "#spawn-machine")
    def submit_machine(self, event: Input.Submitted) -> None:
        self.query_one("#spawn-model", Input).focus()

    @on(Input.Submitted, "#spawn-model")
    def submit_model(self, event: Input.Submitted) -> None:
        name = self.query_one("#spawn-name", Input).value.strip()
        if not name:
            self.notify("Name is required", severity="error")
            return
        machine = self.query_one("#spawn-machine", Input).value.strip() or None
        model = self.query_one("#spawn-model", Input).value.strip() or None
        self.dismiss({"name": name, "machine_name": machine, "model": model})

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Confirm dialog
# ---------------------------------------------------------------------------


class ConfirmDialog(ModalScreen[bool]):
    CSS = """
    ConfirmDialog { align: center middle; }
    #confirm-dialog {
        width: 50; height: auto; max-height: 10;
        border: thick $warning; background: $surface; padding: 1 2;
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
    CSS = """
    PathDialog { align: center middle; }
    #path-dialog {
        width: 60; height: auto; max-height: 10;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

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
    TITLE = "Claude Fleet"

    CSS = """
    #main-layout { height: 1fr; }
    #left-panel { width: 30; border-right: solid $primary; padding: 1; }
    #right-panel { width: 1fr; padding: 1; }
    #worker-detail { height: auto; max-height: 12; margin-bottom: 1; }
    #log-panel { height: 1fr; border: solid $primary; overflow-y: auto; }
    #prompt-input { dock: bottom; margin-top: 1; }
    .worker-idle { color: green; }
    .worker-working { color: yellow; }
    .worker-errored { color: red; }
    .worker-stopped { color: $text-muted; }
    .worker-spawning, .worker-provisioning { color: cyan; }
    """

    BINDINGS = [
        Binding("s", "spawn", "Spawn", show=True),
        Binding("k", "kill_worker", "Kill", show=True),
        Binding("a", "attach_worker", "Attach", show=True),
        Binding("i", "interrupt_worker", "Interrupt", show=True),
        Binding("f", "send_files", "Send", show=True),
        Binding("c", "collect_files", "Collect", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._config: FleetConfig | None = None
        self._state = FleetState()
        self._selected_worker: str | None = None

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
        self._state = FleetState.load()

        new_snapshot = {
            name: worker.status for name, worker in self._state.workers.items()
        }
        if new_snapshot == getattr(self, "_worker_snapshot", None):
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
            item = WorkerListItem(
                Label(f"{status_icon} {name}  {worker.status}", markup=True),
                worker_name=name,
            )
            listview.append(item)

        if old_selection:
            for i, item in enumerate(listview.children):
                if isinstance(item, WorkerListItem) and item.worker_name == old_selection:
                    listview.index = i
                    break

    def _get_selected_worker_name(self) -> str | None:
        listview = self.query_one("#worker-list", ListView)
        child = listview.highlighted_child
        if isinstance(child, WorkerListItem):
            return child.worker_name
        return None

    @on(ListView.Highlighted, "#worker-list")
    def worker_selected(self, event: ListView.Highlighted) -> None:
        if event.item is None:
            return
        name = event.item.worker_name if isinstance(event.item, WorkerListItem) else None
        if name:
            self._selected_worker = name
            self._update_detail(name)
            self._start_log_stream(name)

    def _update_detail(self, name: str) -> None:
        worker = self._state.workers.get(name)
        if not worker:
            self.query_one("#worker-detail", Static).update("")
            return

        machine = self._state.machines.get(worker.machine_name)
        provider = machine.provider if machine else "-"
        ip = machine.ip if machine else "-"

        prompt_display = ""
        if worker.last_prompt:
            truncated = worker.last_prompt[:60] + "..." if len(worker.last_prompt) > 60 else worker.last_prompt
            prompt_display = f"\n  Last: \"{truncated}\""

        detail = (
            f"[bold]{worker.name}[/bold]\n"
            f"  Machine:  {worker.machine_name}  ({provider})\n"
            f"  IP:       {ip}\n"
            f"  Port:     {worker.relay_port}\n"
            f"  Model:    {worker.model}\n"
            f"  Repos:    {', '.join(worker.repos) if worker.repos else '-'}\n"
            f"  Status:   {worker.status}"
            f"{prompt_display}"
        )
        self.query_one("#worker-detail", Static).update(detail)

    # ------------------------------------------------------------------
    # Log streaming
    # ------------------------------------------------------------------

    @work(exclusive=True, group="log_stream")
    async def _start_log_stream(self, name: str) -> None:
        log_widget = self.query_one("#log-content", Static)
        log_scroll = self.query_one("#log-panel", VerticalScroll)

        worker = self._state.workers.get(name)
        if not worker or worker.status in ("stopped", "spawning"):
            log_widget.update("[dim]No logs available[/dim]")
            return

        from cfleet.engine import FleetEngine
        from cfleet.relay_client import format_message

        engine = FleetEngine(config=self._config)
        seen_count = 0

        while self._selected_worker == name:
            try:
                result = await asyncio.to_thread(
                    engine.messages, name, 0, 200
                )
                messages = result.get("messages", [])
                if len(messages) != seen_count:
                    seen_count = len(messages)
                    parts = []
                    for msg in messages:
                        formatted = format_message(msg)
                        if formatted.strip():
                            parts.append(formatted)
                    log_widget.update("\n".join(parts))
                    log_scroll.scroll_end(animate=False)

                self._state = FleetState.load()
                self._refresh_workers()
            except Exception as e:
                log_widget.update(f"[red]Server error: {e}[/red]")
            await asyncio.sleep(2.0)

    # ------------------------------------------------------------------
    # Prompt input
    # ------------------------------------------------------------------

    @on(Input.Submitted, "#prompt-input")
    def send_prompt(self, event: Input.Submitted) -> None:
        name = self._selected_worker
        prompt_text = event.value.strip()
        if not name or not prompt_text:
            return
        event.input.value = ""
        self._do_send_prompt(name, prompt_text)

    @work(thread=True)
    def _do_send_prompt(self, name: str, prompt: str) -> None:
        from cfleet.engine import FleetEngine

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
        def on_result(result: dict | None) -> None:
            if result:
                self._do_spawn(result)
        self.push_screen(SpawnDialog(), callback=on_result)

    @work(thread=True)
    def _do_spawn(self, params: dict) -> None:
        from cfleet.engine import FleetEngine

        try:
            engine = FleetEngine(config=self._config)
            engine.spawn(
                name=params["name"],
                machine_name=params.get("machine_name"),
                model=params.get("model"),
            )
            self.call_from_thread(self.notify, f"Worker {params['name']} spawned")
            self.call_from_thread(self._refresh_workers)
        except Exception as e:
            self.call_from_thread(self.notify, f"Spawn failed: {e}", severity="error")

    def action_kill_worker(self) -> None:
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
        from cfleet.engine import FleetEngine

        try:
            engine = FleetEngine(config=self._config)
            engine.kill(name, force=True)
            self.call_from_thread(self.notify, f"Worker {name} destroyed")
            self.call_from_thread(self._refresh_workers)
        except Exception as e:
            self.call_from_thread(self.notify, f"Kill failed: {e}", severity="error")

    def action_interrupt_worker(self) -> None:
        name = self._get_selected_worker_name()
        if not name:
            self.notify("No worker selected", severity="warning")
            return
        self._do_interrupt(name)

    @work(thread=True)
    def _do_interrupt(self, name: str) -> None:
        from cfleet.engine import FleetEngine

        try:
            engine = FleetEngine(config=self._config)
            engine.interrupt(name)
            self.call_from_thread(self.notify, f"Interrupted {name}")
            self.call_from_thread(self._refresh_workers)
        except Exception as e:
            self.call_from_thread(self.notify, f"Interrupt failed: {e}", severity="error")

    def action_attach_worker(self) -> None:
        name = self._get_selected_worker_name()
        if not name:
            self.notify("No worker selected", severity="warning")
            return

        worker = self._state.workers.get(name)
        if not worker:
            self.notify("Worker not found", severity="warning")
            return

        machine = self._state.machines.get(worker.machine_name)
        if not machine or not (machine.ip or machine.container_id):
            self.notify("Machine not reachable", severity="warning")
            return

        with self.suspend():
            import subprocess
            if machine.provider == "devcontainer":
                subprocess.run([
                    "docker", "exec", "-it", "-u", "vscode",
                    machine.container_id,
                    "bash", "-l",
                ])
            else:
                ssh_key = str(self._config.resolve_ssh_key()) if self._config else "~/.ssh/id_ed25519"
                ssh_user = machine.ssh_user or (self._config.resolve_ssh_user() if self._config else "ubuntu")
                subprocess.run([
                    "ssh", "-t",
                    "-i", ssh_key,
                    "-o", "StrictHostKeyChecking=no",
                    f"{ssh_user}@{machine.ip}",
                    "bash", "-l",
                ])

    def action_send_files(self) -> None:
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
        from cfleet.engine import FleetEngine

        try:
            engine = FleetEngine(config=self._config)
            engine.send(name, local_path)
            self.call_from_thread(self.notify, f"Files sent to {name}")
        except Exception as e:
            self.call_from_thread(self.notify, f"Send failed: {e}", severity="error")

    def action_collect_files(self) -> None:
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
        from cfleet.engine import FleetEngine

        try:
            engine = FleetEngine(config=self._config)
            engine.collect(name, local_dest)
            self.call_from_thread(self.notify, f"Files collected from {name}")
        except Exception as e:
            self.call_from_thread(self.notify, f"Collect failed: {e}", severity="error")
