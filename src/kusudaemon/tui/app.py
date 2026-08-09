"""The Kusudaemon TUI (PLAN.md §11 control surface) — replaces the deleted
web view. ``pip install "kusudaemon[tui]"``, then ``kusudaemon tui`` (or
bare ``kusudaemon``).

Everything the web view could do, the TUI can do too, plus two things the
web view never had: a live diff view for repaired/re-validated artifacts
(``rendering.diff_lines``), and the ability to send a message into a
*currently running* Writer/repair/research subagent's gptme session
mid-episode (``state.RunState.interject`` — see that module's docstring
for how gptme's own external prompt-queue file makes this possible without
forking gptme's chat loop).

Layout is deliberately dense ("this may end up feeling more like a web app
built in the terminal" — and that's fine): a run sidebar on the left,
phase strip along the top of the content area, and a tabbed body (Tree /
Subagents / Approvals / Contract / Spec / Spine / Assembly / Events). Tree
and Subagents both host the same ``NodeDetail`` widget, keyed by whatever
id the table row selection currently points at — a tree node id in one
case, a possibly-derived subagent id (``<node>~repair2``,
``<node>~research~slug``) in the other. ``NodeDetail`` degrades
gracefully when the id isn't a tree node (no gates/judgment to show, just
role/status/trace/interject).

Poll-driven, not push-driven: ``on_mount`` starts a 1s timer calling
``refresh()``, which re-reads ``state.snapshot()`` (always fresh off disk,
per ``state.py``'s own design) and pushes the result into whichever
widgets are currently mounted. Tables only rebuild their rows when the
underlying data actually changed, so an operator mid-keystroke in an
Input/TextArea never gets interrupted by a redraw.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from . import rendering
from .state import RunState

_STATUS_ICON = {True: "●", False: "○"}


def _style(text: str, status: str) -> Text:
    return Text(text, style=rendering.status_style(status))


# ======================================================================
# New-run modal
# ======================================================================
class NewRunScreen(ModalScreen[dict[str, Any] | None]):
    """A goal/source/model form. Submitting dismisses with a body dict for
    ``RunState.start_run``; Escape dismisses with ``None``."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="new-run-dialog"):
            yield Label("New run (or resume: reuse an existing run id)", classes="dialog-title")
            yield Label("Run id (blank = new)")
            yield Input(placeholder="leave blank to generate", id="nr-run-id")
            yield Label("Goal (or @path/to/file)")
            yield Input(placeholder="Summarize the files in this directory.", id="nr-goal")
            yield Label("Source (text, @path, or blank)")
            yield Input(placeholder="@README.md", id="nr-source")
            yield Label("Model (blank = provider default)")
            yield Input(placeholder="", id="nr-model")
            yield Label("Compile command (optional)")
            yield Input(placeholder="", id="nr-compile")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Start", id="nr-start", variant="primary")
                yield Button("Cancel", id="nr-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "nr-cancel":
            self.dismiss(None)
            return
        if event.button.id == "nr-start":
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        from ..pipeline.run import _read_text_arg

        run_id = self.query_one("#nr-run-id", Input).value.strip()
        goal_raw = self.query_one("#nr-goal", Input).value.strip()
        source_raw = self.query_one("#nr-source", Input).value.strip()
        model = self.query_one("#nr-model", Input).value.strip()
        compile_command = self.query_one("#nr-compile", Input).value.strip()
        body = {
            "run_id": run_id,
            "goal": _read_text_arg(goal_raw),
            "source": _read_text_arg(source_raw),
            "model": model or None,
            "compile_command": compile_command or None,
        }
        self.dismiss(body)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ======================================================================
# Sidebar: run browser
# ======================================================================
class Sidebar(Vertical):
    def compose(self) -> ComposeResult:
        yield Label("RUNS", classes="section-title")
        yield DataTable(id="run-table", cursor_type="row")
        with Horizontal(classes="sidebar-buttons"):
            yield Button("New", id="sb-new-run", variant="primary")
            yield Button("Halt", id="sb-halt")
        yield Static("", id="sb-status")

    def on_mount(self) -> None:
        table = self.query_one("#run-table", DataTable)
        table.add_columns("run", "phase", "status")
        self._last: list[tuple[str, ...]] = []

    def update_runs(self, runs: list[dict[str, Any]]) -> None:
        rows = [(r["id"], r["phase"] or "-", r["status"] or "-") for r in runs]
        if rows == self._last:
            return
        self._last = rows
        table = self.query_one("#run-table", DataTable)
        table.clear()
        for run, (run_id, phase, status) in zip(runs, rows):
            table.add_row(
                Text(run_id, style="bold" if run.get("attached") else ""),
                phase,
                _style(status, status),
                key=run_id,
            )

    def update_status(self, snap: dict[str, Any]) -> None:
        status = self.query_one("#sb-status", Static)
        halt_btn = self.query_one("#sb-halt", Button)
        if not snap.get("attached"):
            status.update("(not attached)")
            halt_btn.disabled = True
            return
        halt_btn.disabled = False
        halt_btn.label = "Resume" if snap.get("halted") else "Halt"
        bits = [f"run: {snap.get('run_id')}"]
        if snap.get("hosted"):
            bits.append("[hosted here]")
        if snap.get("halted"):
            bits.append("[HALTED]")
        status.update("\n".join(bits))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "run-table" and event.row_key.value:
            self.app.attach_run(str(event.row_key.value))  # type: ignore[attr-defined]


# ======================================================================
# Phase strip
# ======================================================================
class PhaseStrip(Static):
    def update_phase(self, current_phase: str, phases: dict[str, str], detail: str = "") -> None:
        parts = rendering.phase_strip_text(current_phase, phases)
        text = Text("  ")
        for i, (label, style) in enumerate(parts):
            if i:
                text.append("   ")
            text.append(label, style=style)
        if detail:
            text.append(f"   — {detail}", style="dim")
        self.update(text)


# ======================================================================
# Node / subagent detail panel (shared by the Tree and Subagents tabs)
# ======================================================================
class NodeDetail(Vertical):
    """Shows whatever ``node_id`` currently points at: for a real tree
    node, gates/judgment/inputs/artifact/diff history; for a
    repair/research/pilot subagent id, role/status/trace only. Both cases
    get a live "message" box when the underlying gptme session is
    discovered and still running (``state.node_gptme_logdir``)."""

    def __init__(self, *, panel_id: str) -> None:
        super().__init__(id=panel_id)
        self.node_id: str | None = None

    def compose(self) -> ComposeResult:
        with TabbedContent(id=f"{self.id}-tabs"):
            with TabPane("Overview", id=f"{self.id}-overview"):
                yield VerticalScroll(Static("(select a row)", id=f"{self.id}-overview-text"))
            with TabPane("Artifact", id=f"{self.id}-artifact"):
                yield VerticalScroll(Static("", id=f"{self.id}-artifact-text"))
            with TabPane("Diff", id=f"{self.id}-diff"):
                yield VerticalScroll(Static("", id=f"{self.id}-diff-text"))
            with TabPane("Thinking", id=f"{self.id}-trace"):
                yield VerticalScroll(Static("", id=f"{self.id}-trace-text"))
        yield Label("Message the running agent (mid-episode):", classes="bar-label")
        with Horizontal(id=f"{self.id}-interject-bar", classes="interject-bar"):
            yield Input(placeholder="(not live)", id=f"{self.id}-interject-input", disabled=True)
            yield Button("Send", id=f"{self.id}-interject-send", disabled=True)
        yield Label("Reopen (passed nodes only): defect to fix", classes="bar-label")
        with Horizontal(id=f"{self.id}-reopen-bar", classes="interject-bar"):
            yield Input(placeholder="(only for passed nodes)", id=f"{self.id}-reopen-input", disabled=True)
            yield Button("Reopen", id=f"{self.id}-reopen-send", disabled=True)

    def select(self, node_id: str | None) -> None:
        if node_id == self.node_id:
            return
        self.node_id = node_id
        self.refresh_content()

    def refresh_content(self) -> None:
        node_id = self.node_id
        overview = self.query_one(f"#{self.id}-overview-text", Static)
        artifact_w = self.query_one(f"#{self.id}-artifact-text", Static)
        diff_w = self.query_one(f"#{self.id}-diff-text", Static)
        trace_w = self.query_one(f"#{self.id}-trace-text", Static)
        interject_in = self.query_one(f"#{self.id}-interject-input", Input)
        interject_btn = self.query_one(f"#{self.id}-interject-send", Button)
        reopen_in = self.query_one(f"#{self.id}-reopen-input", Input)
        reopen_btn = self.query_one(f"#{self.id}-reopen-send", Button)

        if not node_id:
            overview.update("(select a row)")
            artifact_w.update("")
            diff_w.update("")
            trace_w.update("")
            interject_in.disabled = True
            interject_btn.disabled = True
            reopen_in.disabled = True
            reopen_btn.disabled = True
            return

        state: RunState = self.app.state  # type: ignore[attr-defined]
        detail = state.node_detail(node_id)
        artifact_text = state.artifact(node_id) or ""

        reopenable = detail is not None and detail.get("status") == "passed"
        reopen_in.disabled = not reopenable
        reopen_btn.disabled = not reopenable
        reopen_in.placeholder = "describe what's wrong…" if reopenable else "(only for passed nodes)"

        if detail is not None:
            overview.update(_render_overview(detail))
            artifact_w.update(artifact_text or "(empty)")
            diff_w.update(_render_diff_history(state, node_id, detail, artifact_text))
        else:
            subagent = next((s for s in state.subagents() if s["id"] == node_id), None)
            overview.update(_render_subagent_overview(subagent, node_id))
            artifact_w.update("(not a tree node — no standalone artifact)")
            diff_w.update("(no version history for a subagent id — see its parent tree node)")

        trace_w.update(_render_trace(state.trace(node_id) or ""))

        logdir = state.node_gptme_logdir(node_id)
        completed = detail is not None and detail.get("status") not in ("dispatched", "awaiting_review", "pending")
        subagents_by_id = {s["id"]: s for s in state.subagents()}
        sub = subagents_by_id.get(node_id)
        live = bool(logdir) and (sub is None or sub.get("live", not completed))
        interject_in.disabled = not live
        interject_btn.disabled = not live
        interject_in.placeholder = "Message the running agent…" if live else "(not currently running)"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == f"{self.id}-interject-send":
            self._send_interject()
        elif event.button.id == f"{self.id}-reopen-send":
            self._send_reopen()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == f"{self.id}-interject-input":
            self._send_interject()
        elif event.input.id == f"{self.id}-reopen-input":
            self._send_reopen()

    def _send_interject(self) -> None:
        if not self.node_id:
            return
        field = self.query_one(f"#{self.id}-interject-input", Input)
        text = field.value.strip()
        if not text:
            return
        state: RunState = self.app.state  # type: ignore[attr-defined]
        ok = state.interject(self.node_id, text)
        if ok:
            field.value = ""
            self.app.notify(f"queued for {self.node_id}", title="Message sent")  # type: ignore[attr-defined]
        else:
            self.app.notify("no live session found for this node", title="Not sent", severity="warning")  # type: ignore[attr-defined]

    def _send_reopen(self) -> None:
        if not self.node_id:
            return
        field = self.query_one(f"#{self.id}-reopen-input", Input)
        text = field.value.strip()
        if not text:
            return
        state: RunState = self.app.state  # type: ignore[attr-defined]
        approval = state.request_reopen(self.node_id, text)
        if approval:
            field.value = ""
            self.app.notify("reopen queued — approve it in the Approvals tab", title="Reopen")  # type: ignore[attr-defined]
        else:
            self.app.notify("could not queue reopen", title="Reopen", severity="warning")  # type: ignore[attr-defined]


def _render_overview(detail: dict[str, Any]) -> Text:
    t = Text()
    t.append(f"{detail['id']}  ", style="bold")
    t.append(detail.get("status", ""), style=rendering.status_style(detail.get("status", "")))
    t.append(f"   attempts={detail.get('attempts', 0)}   shape={detail.get('shape', '')}\n\n")
    if detail.get("brief"):
        t.append(detail["brief"] + "\n\n")
    t.append("gates: ", style="bold")
    t.append(", ".join(detail.get("gates") or []) or "(none)")
    t.append("\n")
    for gr in detail.get("gate_results") or []:
        icon = "✓" if gr["passed"] else "✗"
        style = "green" if gr["passed"] else "red"
        t.append(f"  {icon} {gr['gate']}", style=style)
        if gr.get("detail"):
            t.append(f" — {gr['detail']}", style="dim")
        t.append("\n")
    if detail.get("judgment"):
        t.append("\njudgment:\n", style="bold")
        for jid in detail["judgment"]:
            rubric = (detail.get("rubric") or {}).get(jid, "")
            t.append(f"  - {jid}: {rubric}\n")
    manifest = detail.get("manifest")
    if manifest:
        t.append("\nreviewer verdict: ", style="bold")
        t.append(str(manifest.get("verdict", manifest.get("gates", ""))) + "\n")
        if manifest.get("promotion"):
            t.append("promotion: ", style="bold")
            t.append(str(manifest["promotion"]) + "\n")
    if detail.get("inputs"):
        t.append("\ninputs:\n", style="bold")
        for item in detail["inputs"]:
            mark = "✓" if item.get("exists") else "✗"
            t.append(f"  {mark} {item['ref']} ({item.get('tokens', 0)} tok)\n")
    if detail.get("depends_on"):
        t.append("\ndepends_on: ", style="bold")
        t.append(", ".join(detail["depends_on"]) + "\n")
    t.append(f"\nbudget: {detail.get('budget', {}).get('tokens')} tokens, {detail.get('budget', {}).get('calls')} calls\n")
    t.append(f"artifact tokens: {detail.get('artifact_tokens', 0)}\n")
    if detail.get("versions"):
        t.append(f"versions: {', '.join(detail['versions'])}\n")
    return t


def _render_subagent_overview(subagent: dict[str, Any] | None, node_id: str) -> Text:
    t = Text()
    t.append(f"{node_id}  ", style="bold")
    if subagent is None:
        t.append("(no event record yet)")
        return t
    t.append(subagent.get("status", ""), style=rendering.status_style(subagent.get("status", "")))
    t.append(f"\nkind: {subagent.get('kind')}   role: {subagent.get('role')}\n")
    t.append(f"attempts: {subagent.get('attempts', 0)}   duration: {subagent.get('duration_ms', 0)}ms\n")
    if subagent.get("live"):
        t.append("● live — send it a message below\n", style="bold yellow")
    if subagent.get("error"):
        t.append(f"error: {subagent['error']}\n", style="red")
    return t


def _render_trace(raw: str) -> Text:
    entries = rendering.parse_trace(raw)
    if not entries:
        return Text("(no trace yet)", style="dim")
    t = Text()
    for entry in entries:
        t.append(f"[{entry.role}] ", style=rendering.role_style(entry.role))
        t.append(entry.text + "\n")
    return t


def _render_diff_history(state: RunState, node_id: str, detail: dict[str, Any], current: str) -> Text:
    versions = detail.get("versions") or []
    if not versions:
        return Text("(no prior versions — nothing to diff yet)", style="dim")
    t = Text()
    for tag in reversed(versions):
        old_text = state.version_snapshot(node_id, tag) or ""
        t.append(f"── {tag} → current ──\n", style="bold cyan")
        for line in rendering.diff_lines(old_text, current, old_label=tag, new_label="current"):
            style = rendering.DIFF_LINE_STYLE.get(line.kind, "")
            t.append(line.text + "\n", style=style)
        t.append("\n")
    return t


# ======================================================================
# Node / subagent tables
# ======================================================================
class NodeTable(DataTable):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_columns("id", "status", "shape", "attempts", "gates", "judgment", "brief")
        self._last: list[tuple] = []

    def update_nodes(self, nodes: list[dict[str, Any]]) -> None:
        rows = [
            (n["id"], n["status"], n["shape"], n["attempts"], n["gates"], n["judgment"], n["brief"][:60])
            for n in nodes
        ]
        if rows == self._last:
            return
        self._last = rows
        self.clear()
        for n, row in zip(nodes, rows):
            self.add_row(
                row[0],
                _style(row[1], row[1]),
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
                key=n["id"],
            )


class SubagentTable(DataTable):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_columns("id", "kind", "role", "status", "attempts", "duration", "live")
        self._last: list[tuple] = []

    def update_subagents(self, subagents: list[dict[str, Any]]) -> None:
        rows = [
            (s["id"], s["kind"], s["role"], s["status"], s["attempts"], s["duration_ms"], s["live"])
            for s in subagents
        ]
        if rows == self._last:
            return
        self._last = rows
        self.clear()
        for s, row in zip(subagents, rows):
            self.add_row(
                row[0],
                row[1],
                row[2],
                _style(row[3], row[3]),
                row[4],
                f"{row[5]}ms" if row[5] else "-",
                _STATUS_ICON[bool(row[6])],
                key=s["id"],
            )


# ======================================================================
# Approvals
# ======================================================================
class ApprovalCard(Vertical):
    def __init__(self, approval: dict[str, Any]) -> None:
        super().__init__(id=f"approval-{approval['approval_id']}", classes="approval-card")
        self.approval = approval

    def compose(self) -> ComposeResult:
        a = self.approval
        yield Label(a["title"], classes="approval-title")
        yield Static(a.get("message", ""), classes="approval-message")
        if a.get("allow_input"):
            yield TextArea(id=f"approval-input-{a['approval_id']}", classes="approval-input")
        with Horizontal(classes="approval-buttons"):
            options = a.get("options") or []
            if not options and a.get("allow_input"):
                options = [{"value": "answer", "label": "Submit", "style": "primary"}]
            for opt in options:
                yield Button(
                    opt.get("label", opt.get("value", "ok")),
                    id=f"approval-btn-{a['approval_id']}-{opt.get('value', 'ok')}",
                    variant="primary" if opt.get("style") == "primary" else "default",
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        prefix = f"approval-btn-{self.approval['approval_id']}-"
        if not (event.button.id or "").startswith(prefix):
            return
        action = event.button.id[len(prefix):]
        text = ""
        if self.approval.get("allow_input"):
            try:
                text = self.query_one(f"#approval-input-{self.approval['approval_id']}", TextArea).text
            except Exception:
                text = ""
        state: RunState = self.app.state  # type: ignore[attr-defined]
        state.resolve_approval(self.approval["approval_id"], action=action, user_input=text)
        self.app.notify(f"resolved: {self.approval['title']}")  # type: ignore[attr-defined]


class ApprovalsPane(VerticalScroll):
    def __init__(self) -> None:
        super().__init__(id="approvals-pane")
        self._ids: list[str] = []

    def update_approvals(self, pending: list[dict[str, Any]]) -> None:
        ids = [a["approval_id"] for a in pending]
        if ids == self._ids:
            return
        self._ids = ids
        for child in list(self.children):
            child.remove()
        if not pending:
            self.mount(Static("(no pending approvals)", classes="dim"))
            return
        for approval in pending:
            self.mount(ApprovalCard(approval))


# ======================================================================
# Main app
# ======================================================================
class KusudaemonApp(App):
    TITLE = "kusudaemon"
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    Sidebar { width: 34; border-right: solid $accent; padding: 1; }
    .section-title { text-style: bold; color: $accent; }
    #run-table { height: 12; }
    .sidebar-buttons { height: 3; }
    .sidebar-buttons Button { width: 1fr; }
    #sb-status { color: $text-muted; margin-top: 1; }
    #content { padding: 0 1; }
    PhaseStrip { height: 1; margin-bottom: 1; }
    .tree-split, .subagent-split { height: 1fr; }
    .tree-split > DataTable, .subagent-split > DataTable { width: 46%; }
    NodeDetail { width: 1fr; border-left: solid $accent; padding: 0 1; }
    .bar-label { color: $text-muted; height: 1; margin-top: 1; }
    .interject-bar { height: 3; }
    .interject-bar Input { width: 1fr; }
    .approval-card { border: round $accent; padding: 1; margin-bottom: 1; height: auto; }
    .approval-title { text-style: bold; }
    .approval-input { height: 6; margin: 1 0; }
    .approval-buttons Button { margin-right: 1; }
    #new-run-dialog { width: 70; height: auto; border: thick $accent; padding: 1 2; background: $panel; }
    .dialog-title { text-style: bold; margin-bottom: 1; }
    .dialog-buttons { margin-top: 1; height: 3; }
    .dim { color: $text-muted; }
    #events-log { height: 1fr; }
    """

    BINDINGS = [
        Binding("n", "new_run", "New run"),
        Binding("H", "toggle_halt", "Halt/Resume"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, runs_root: str | Path = "./.kusudaemon/runs", attach_run_id: str | None = None) -> None:
        super().__init__()
        self.state = RunState(runs_root)
        self._initial_attach = attach_run_id
        self._last_events_count = 0
        self._selected_tree_node: str | None = None
        self._selected_subagent: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield Sidebar(id="sidebar")
            with Vertical(id="content"):
                yield PhaseStrip(id="phase-strip")
                with TabbedContent(id="main-tabs"):
                    with TabPane("Tree", id="tab-tree"):
                        with Horizontal(classes="tree-split"):
                            yield NodeTable(id="node-table")
                            yield NodeDetail(panel_id="tree-detail")
                    with TabPane("Subagents", id="tab-subagents"):
                        with Horizontal(classes="subagent-split"):
                            yield SubagentTable(id="subagent-table")
                            yield NodeDetail(panel_id="sub-detail")
                    with TabPane("Approvals", id="tab-approvals"):
                        yield ApprovalsPane()
                    with TabPane("Contract", id="tab-contract"):
                        yield VerticalScroll(Static("", id="contract-text"))
                        with Horizontal(classes="interject-bar"):
                            yield Input(placeholder="rule text to append…", id="amend-input")
                            yield Button("Amend", id="amend-send")
                    with TabPane("Spec", id="tab-spec"):
                        yield VerticalScroll(Static("", id="spec-text"))
                    with TabPane("Spine", id="tab-spine"):
                        yield VerticalScroll(Static("", id="spine-text"))
                    with TabPane("Assembly", id="tab-assembly"):
                        yield VerticalScroll(Static("", id="assembly-text"))
                    with TabPane("Events", id="tab-events"):
                        yield RichLog(id="events-log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        if self._initial_attach:
            self.state.attach(self._initial_attach)
        self.set_interval(1.0, self.refresh_all)
        self.refresh_all()

    # ------------------------------------------------------------------
    def attach_run(self, run_id: str) -> None:
        self.state.attach(run_id)
        self._last_events_count = 0
        self.query_one("#events-log", RichLog).clear()
        self.refresh_all()

    def refresh_all(self) -> None:
        snap = self.state.snapshot()
        self.query_one(Sidebar).update_runs(snap["runs"])
        self.query_one(Sidebar).update_status(snap)
        if not snap.get("attached"):
            return
        self.query_one(PhaseStrip).update_phase(snap.get("phase", ""), snap.get("phases", {}), snap.get("phase_detail", ""))
        self.query_one("#node-table", NodeTable).update_nodes(snap.get("tree", []))
        self.query_one("#subagent-table", SubagentTable).update_subagents(snap.get("subagents", []))
        self.query_one("#tree-detail", NodeDetail).refresh_content()
        self.query_one("#sub-detail", NodeDetail).refresh_content()
        self.query_one(ApprovalsPane).update_approvals(snap.get("pending_approvals", []))
        self.query_one("#contract-text", Static).update(self.state.contract_text() or "(no contract yet)")
        self.query_one("#spec-text", Static).update(self.state.spec_text() or "(no spec yet)")
        self.query_one("#spine-text", Static).update(self.state.spine_text() or "(no spine yet)")
        self._update_assembly()
        self._update_events(snap.get("events", []), snap.get("events_count", 0))

    def _update_assembly(self) -> None:
        assembly = self.state.assembly()
        text = Text()
        if assembly.get("checks"):
            text.append("checks:\n", style="bold")
            text.append(str(assembly["checks"]) + "\n\n")
        if assembly.get("compile_log"):
            text.append("compile log:\n", style="bold")
            text.append(assembly["compile_log"] + "\n\n")
        if assembly.get("output"):
            text.append("output:\n", style="bold")
            text.append(assembly["output"])
        self.query_one("#assembly-text", Static).update(text if text.plain else "(not assembled yet)")

    def _update_events(self, events: list[dict[str, Any]], total: int) -> None:
        if total <= self._last_events_count:
            return
        log = self.query_one("#events-log", RichLog)
        new_events = events[-(total - self._last_events_count):] if total - self._last_events_count <= len(events) else events
        for event in new_events:
            log.write(f"[dim]{event.get('node_id', '-')}[/] {event.get('type', '')} {_event_extra(event)}")
        self._last_events_count = total

    # ------------------------------------------------------------------
    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        node_id = str(event.row_key.value) if event.row_key.value else None
        if event.data_table.id == "node-table":
            self.query_one("#tree-detail", NodeDetail).select(node_id)
        elif event.data_table.id == "subagent-table":
            self.query_one("#sub-detail", NodeDetail).select(node_id)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sb-new-run":
            self.action_new_run()
        elif event.button.id == "sb-halt":
            self.action_toggle_halt()
        elif event.button.id == "amend-send":
            self._send_amend()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "amend-input":
            self._send_amend()

    def _send_amend(self) -> None:
        field = self.query_one("#amend-input", Input)
        text = field.value.strip()
        if not text:
            return
        approval = self.state.request_amend(text)
        field.value = ""
        if approval:
            self.notify("amendment queued as a pending approval", title="Amend")
        else:
            self.notify("not attached to a run", title="Amend", severity="warning")

    # ------------------------------------------------------------------
    def action_new_run(self) -> None:
        def _on_result(body: dict[str, Any] | None) -> None:
            if not body:
                return
            run_id, error = self.state.start_run(body)
            if error:
                self.notify(error, title="New run failed", severity="error")
            else:
                self.notify(f"started {run_id}", title="New run")
                self.attach_run(run_id)  # type: ignore[arg-type]

        self.push_screen(NewRunScreen(), _on_result)

    def action_toggle_halt(self) -> None:
        snap = self.state.snapshot()
        if not snap.get("attached"):
            return
        self.state.halt(not snap.get("halted"))
        self.refresh_all()

    def action_quit(self) -> None:
        self.exit()


def _event_extra(event: dict[str, Any]) -> str:
    bits = []
    for key in ("phase", "status", "reason", "kind"):
        if event.get(key):
            bits.append(f"{key}={event[key]}")
    return " ".join(bits)
