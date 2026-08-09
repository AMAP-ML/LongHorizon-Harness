"""Command handlers for the ``kusudaemon pipeline`` group (PLAN.md §11's
control surface: run / status / approve / amend / resume).

The run command drives :class:`RecursiveDriver` in the foreground by
default (with the web view optionally served alongside it), or detaches a
background subprocess for pure ``status`` / ``approve`` / web-attach
workflows. Approve and amend operate purely on the run directory's
``approvals.jsonl`` and ``contract.md`` — which is exactly what makes
them safe to run from a second terminal while the driver (or a web view)
is still attached to the same run (§11: the view "can be attached from
anywhere").
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from ..environment.local import LocalEnvironment
from ..v0.events import EventLog
from ..v1.provider import OpenAICompatibleProvider
from ..v1.tree import TaskTree
from . import approvals as approval_store
from .backends import build_writer_adapter
from .driver import RunOptions, amend_and_revalidate, apply_triage
from .run_dir import (
    contract_path,
    events_path,
    phase_path,
    run_spec_path,
    tree_path,
)

_RUNS_ROOT_DEFAULT = "./.kusudaemon/runs"


def build_pipeline_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kusudaemon pipeline",
        description="PLAN.md §11 control surface for the recursive decomposition harness.",
    )
    sub = parser.add_subparsers(dest="pipeline_command")

    run_parser = sub.add_parser("run", help="Run (or resume) the pipeline.")
    run_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)
    run_parser.add_argument("--run-id", default=None, help="Reuse an id to resume an existing run dir.")
    run_parser.add_argument("--goal", default="", help="Task goal (or @path).")
    run_parser.add_argument("--source", default="", help="Source document: text, @file, or -.")
    run_parser.add_argument("--backend", default="gptme", choices=("gptme",))
    run_parser.add_argument("--model", default=None)
    run_parser.add_argument("--compile-command", default=None)
    run_parser.add_argument("--research-plan", default=None)
    run_parser.add_argument("--max-rounds", type=int, default=100)
    run_parser.add_argument("--max-attempts", type=int, default=3)
    run_parser.add_argument("--detach", action="store_true", help="Run in a background subprocess and return immediately.")
    run_parser.add_argument("--dashboard", action="store_true", help="Also serve the web view (PLAN.md §11) alongside this run, on a background thread.")
    run_parser.add_argument("--dashboard-host", default="127.0.0.1")
    run_parser.add_argument("--dashboard-port", type=int, default=8765)

    resume_parser = sub.add_parser("resume", help="Resume a run after a halt or crash.")
    resume_parser.add_argument("run_id")
    resume_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)

    status_parser = sub.add_parser("status", help="Print phase, tree, and pending approvals.")
    status_parser.add_argument("run_id")
    status_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)

    approve_parser = sub.add_parser("approve", help="Resolve the oldest pending approval.")
    approve_parser.add_argument("run_id")
    approve_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)
    approve_parser.add_argument("--answer", default="", help="Free-form answer / edited artifact text.")
    approve_parser.add_argument("--file", default=None, help="Read the answer (e.g. a pilot edit) from a file.")
    approve_parser.add_argument("--action", default="answer", help="Option value for option-based approvals.")

    amend_parser = sub.add_parser("amend", help="Amend the contract and run the re-validation pass.")
    amend_parser.add_argument("run_id")
    amend_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)
    amend_parser.add_argument("--text", required=True, help="The rule text to append to the contract.")
    amend_parser.add_argument("--reason", default="CLI amendment", help="Attribution shown in the contract.")
    amend_parser.add_argument("--yes", action="store_true", help="Apply the triage without prompting.")
    amend_parser.add_argument("--max-attempts", type=int, default=3)

    serve_parser = sub.add_parser("serve", help="Serve the web view (PLAN.md §11) over a runs directory.")
    serve_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--run-id", default=None, help="Attach to this run on startup.")
    serve_parser.add_argument("--no-control", action="store_true", help="Read-only view: disable start/attach/approve/amend/halt/reopen.")
    return parser


def _run_dir(root: str, run_id: str) -> Path:
    return Path(root).expanduser() / run_id


def cmd_run(argv: argparse.Namespace) -> int:
    if argv.detach:
        return cmd_run_detach(argv)
    from .run import run_from_args

    run_id = argv.run_id
    httpd = None
    if getattr(argv, "dashboard", False):
        run_id = run_id or _default_run_id()
        httpd = _start_dashboard(argv, run_id)
    try:
        return run_from_args(_run_argv(argv, run_id=run_id))
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()


def _start_dashboard(argv: argparse.Namespace, run_id: str):
    """Start the web view (PLAN.md §11) on a background thread alongside a
    foreground ``run``, auto-attaching to ``run_id`` as soon as its run
    directory exists (the driver touches ``events.jsonl`` at the very start
    of ``RecursiveDriver.run()``, so this is a short wait, not a race the
    operator would notice)."""
    from ..dashboard.recursive import RecursiveRunState
    from ..dashboard.server import make_server

    state = RecursiveRunState(argv.runs_root, control_enabled=True)
    httpd = make_server(state, argv.dashboard_host, argv.dashboard_port)
    thread = threading.Thread(target=httpd.serve_forever, name="kusudaemon-dashboard", daemon=True)
    thread.start()
    print(f"dashboard: http://{argv.dashboard_host}:{argv.dashboard_port}/  (watching {argv.runs_root})")

    def _auto_attach() -> None:
        for _ in range(200):
            if state.attach(run_id):
                return
            time.sleep(0.25)

    threading.Thread(target=_auto_attach, name="kusudaemon-dashboard-attach", daemon=True).start()
    return httpd


def cmd_serve(argv: argparse.Namespace) -> int:
    from ..dashboard.server import run_forever

    run_forever(
        argv.runs_root,
        argv.host,
        argv.port,
        attach_run_id=argv.run_id,
        control_enabled=not argv.no_control,
    )
    return 0


def cmd_run_detach(argv: argparse.Namespace) -> int:
    run_id = argv.run_id or _default_run_id()
    command = [
        sys.executable,
        "-m",
        "kusudaemon.pipeline.run",
        "--runs-root", argv.runs_root,
        "--run-id", run_id,
        "--goal", argv.goal,
        "--source", argv.source,
        "--backend", argv.backend,
        "--max-rounds", str(argv.max_rounds),
        "--max-attempts", str(argv.max_attempts),
    ]
    for flag, value in (
        ("--model", argv.model),
        ("--compile-command", argv.compile_command),
        ("--research-plan", argv.research_plan),
    ):
        if value:
            command += [flag, value]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    print(f"detached pipeline run: run_id={run_id}")
    print(f"watch with: kusudaemon pipeline status {run_id} --runs-root {argv.runs_root}")
    return 0


def cmd_status(argv: argparse.Namespace) -> int:
    run_dir = _run_dir(argv.runs_root, argv.run_id)
    if not run_dir.is_dir():
        print(f"no such run: {run_dir}", file=sys.stderr)
        return 1
    import json

    phase: dict = {}
    try:
        phase = json.loads(phase_path(run_dir).read_text(encoding="utf-8"))
    except OSError:
        pass
    print(f"run:      {argv.run_id}")
    print(f"phase:    {phase.get('phase', '-')} ({phase.get('status', '-')})")
    if phase.get("detail"):
        print(f"detail:   {phase['detail']}")
    tree = TaskTree.load(tree_path(run_dir)) if tree_path(run_dir).exists() else None
    if tree is not None:
        counts: dict[str, int] = {}
        for node in tree.nodes.values():
            counts[node.status] = counts.get(node.status, 0) + 1
        print(f"tree:     {len(tree.nodes)} nodes; statuses={counts}")
    pending = approval_store.pending(run_dir)
    print(f"approvals: {len(pending)} pending")
    for item in pending[:10]:
        print(f"  - {item.kind} ({item.approval_id}): {item.title}")
    if contract_path(run_dir).exists():
        print(f"contract: {len(contract_path(run_dir).read_text(encoding='utf-8').splitlines())} lines")
    events = EventLog(events_path(run_dir))
    print(f"events:   {len(events.read_all())} recorded")
    return 0


def cmd_approve(argv: argparse.Namespace) -> int:
    run_dir = _run_dir(argv.runs_root, argv.run_id)
    pending = approval_store.pending(run_dir)
    if not pending:
        print("no pending approvals", file=sys.stderr)
        return 1
    target = pending[0]
    answer = Path(argv.file).read_text(encoding="utf-8") if argv.file else argv.answer
    approval_store.append(run_dir, target.resolve(action=argv.action, user_input=answer))
    print(f"resolved {target.kind} approval {target.approval_id}")
    return 0


def cmd_amend(argv: argparse.Namespace) -> int:
    run_dir = _run_dir(argv.runs_root, argv.run_id)
    provider = OpenAICompatibleProvider()
    options = _load_options(run_dir)
    result = asyncio.run(
        amend_and_revalidate(run_dir, rule_text=argv.text, reason=argv.reason, provider=provider)
    )
    counts = result["counts"]
    print(f"revalidation: clean={counts['clean']} patchable={counts['patchable']} regenerate={counts['regenerate']}")
    if not argv.yes:
        try:
            choice = input("apply repairs for non-clean nodes? [y/N] ").strip().lower()
        except EOFError:
            choice = "n"
    else:
        choice = "y"
    if choice not in ("y", "yes"):
        print("not applying; the triage stays recorded under audit/revalidation/")
        return 0
    env = LocalEnvironment(tmp_dir=str(run_dir / "tmp"))
    repaired = asyncio.run(
        apply_triage(
            run_dir,
            triage=result["triage"],
            writer_adapter_factory=_writer_factory(options, run_dir),
            env=env,
            provider=provider,
            max_attempts=argv.max_attempts,
        )
    )
    print(f"repaired: {', '.join(repaired) if repaired else '(none)'}")
    return 0


def cmd_resume(argv: argparse.Namespace) -> int:
    # Resume is exactly "run with an existing run-id": the driver converges
    # from durable state (§10), so there is no separate resume code path —
    # argv contributes nothing but the run id.
    from .run import run_from_args

    return run_from_args(["--runs-root", argv.runs_root, "--run-id", argv.run_id])


def dispatch(args: argparse.Namespace) -> int:
    """Route a parsed pipeline group to its handler."""
    command = args.pipeline_command
    if command == "run":
        return cmd_run(args)
    if command == "resume":
        return cmd_resume(args)
    if command == "status":
        return cmd_status(args)
    if command == "approve":
        return cmd_approve(args)
    if command == "amend":
        return cmd_amend(args)
    if command == "serve":
        return cmd_serve(args)
    raise ValueError(f"unknown pipeline command: {command!r}")


def _load_options(run_dir: Path) -> RunOptions:
    path = run_spec_path(run_dir)
    if not path.exists():
        raise FileNotFoundError(f"no run at {run_dir} (missing run.spec.json)")
    import json

    return RunOptions.from_spec(json.loads(path.read_text(encoding="utf-8")))


def _writer_factory(options: RunOptions, run_dir: Path):
    def factory(node):
        return build_writer_adapter(
            options.backend,
            workspace_path=run_dir,
            prompt_dir=run_dir / "tmp" / "prompts",
            node=node,
            model=options.model,
        )

    return factory


def _run_argv(argv: argparse.Namespace, *, run_id: str | None) -> list[str]:
    parts = [
        "--runs-root", argv.runs_root,
        "--goal", argv.goal,
        "--source", argv.source,
        "--backend", argv.backend,
        "--max-rounds", str(argv.max_rounds),
        "--max-attempts", str(argv.max_attempts),
    ]
    if run_id:
        parts += ["--run-id", run_id]
    for flag, value in (
        ("--model", argv.model),
        ("--compile-command", argv.compile_command),
        ("--research-plan", argv.research_plan),
    ):
        if value:
            parts += [flag, value]
    return parts


def _default_run_id() -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:8]}"