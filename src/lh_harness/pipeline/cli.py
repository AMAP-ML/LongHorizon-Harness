"""Command handlers for the ``lh-harness pipeline`` group (PLAN.md §11's
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

_RUNS_ROOT_DEFAULT = "./.lh-harness/runs"


def build_pipeline_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lh-harness pipeline",
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
    return parser


def _run_dir(root: str, run_id: str) -> Path:
    return Path(root).expanduser() / run_id


def cmd_run(argv: argparse.Namespace) -> int:
    if argv.detach:
        return cmd_run_detach(argv)
    from .run import run_from_args

    return run_from_args(_run_argv(argv, run_id=argv.run_id))


def cmd_run_detach(argv: argparse.Namespace) -> int:
    run_id = argv.run_id or _default_run_id()
    command = [
        sys.executable,
        "-m",
        "lh_harness.pipeline.run",
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
    print(f"watch with: lh-harness pipeline status {run_id} --runs-root {argv.runs_root}")
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