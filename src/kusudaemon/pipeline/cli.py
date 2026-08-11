"""Command handlers for the ``kusudaemon pipeline`` group (PLAN.md §11's
control surface: run / status / approve / amend / resume / serve).

The run command drives :class:`RecursiveDriver` in the foreground by
default, or detaches a background subprocess for pure ``status`` /
``approve`` / ``serve``-attach workflows. Approve and amend operate purely
on the run directory's ``approvals.jsonl`` and ``contract.md`` — which is
exactly what makes them safe to run from a second terminal while the
driver (or the dashboard server) is still attached to the same run (§11:
the view "can be attached from anywhere").
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
from .driver import RunOptions, amend_contract_and_estimate, apply_triage, run_amendment_revalidation
from .liveness import run_liveness
from .run_dir import (
    contract_path,
    events_path,
    phase_path,
    resolve_runs_root,
    run_spec_path,
    source_path,
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
    run_parser.add_argument(
        "--runs-root",
        default=None,
        help=f"Defaults to {_RUNS_ROOT_DEFAULT}, or <workspace>/.kusudaemon/runs "
        "when --workspace is given.",
    )
    run_parser.add_argument("--run-id", default=None, help="Reuse an id to resume an existing run dir.")
    run_parser.add_argument("--goal", default="", help="Task goal (or @path).")
    run_parser.add_argument("--source", default="", help="Source document: text, @file, or -.")
    run_parser.add_argument(
        "--workspace",
        default=None,
        help="Path to a real directory the Writer edits directly "
        "(PLAN.md §A3 kind=\"workspace\") instead of a materialized corpus.",
    )
    run_parser.add_argument("--backend", default="gptme", choices=("gptme",))
    run_parser.add_argument("--model", default=None)
    run_parser.add_argument("--compile-command", default=None)
    run_parser.add_argument("--research-plan", default=None)
    run_parser.add_argument("--max-rounds", type=int, default=100)
    run_parser.add_argument("--max-attempts", type=int, default=3)
    run_parser.add_argument(
        "--max-parallel",
        type=int,
        default=1,
        help="Writer episodes dispatched concurrently per round (PLAN.md §C2); "
        "1 reproduces today's serial behavior exactly.",
    )
    run_parser.add_argument(
        "--dispatch-policy",
        choices=("model", "document_order"),
        default="model",
        help="document_order skips the per-round orchestrator LLM call and "
        "dispatches the earliest ready node in document order (PLAN-zeromem.md §1)",
    )
    run_parser.add_argument(
        "--document-review",
        action="store_true",
        help="Run PLAN-zeromem.md §8 document-level review passes after "
        "assembly; surface triage counts for approval before any repair "
        "dispatches.",
    )
    run_parser.add_argument(
        "--survey-mode",
        choices=("model", "embedding"),
        default="model",
        help="embedding replaces the per-window model survey with "
        "embedding-dissimilarity boundaries (PLAN-zeromem.md §3); "
        "falls back to model when kusudaemon[retrieval] is not installed.",
    )
    run_parser.add_argument(
        "--inline-spans",
        action="store_true",
        help="Inline top-k retrieved spans from the node's own spine slice "
        "into its prompt instead of bare input paths (PLAN-zeromem.md §4).",
    )
    run_parser.add_argument(
        "--tier",
        choices=("T0", "T1", "T2", "T3"),
        default=None,
        help="Force a tier floor, never a ceiling (PLAN.md §A4.4 invariant "
        "9): the classifier still runs; the effective tier is "
        "max(measured, this).",
    )
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

    escalate_parser = sub.add_parser(
        "escalate", help="Promote a run's tier by one (PLAN.md §A4.4 operator intervention)."
    )
    escalate_parser.add_argument("run_id")
    escalate_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)

    serve_parser = sub.add_parser("serve", help="Serve the web dashboard (PLAN.md §11 control surface) over a runs directory.")
    serve_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--run-id", default=None, help="Attach to this run on startup.")
    serve_parser.add_argument("--no-control", action="store_true", help="Read-only view: disable start/attach/halt/approve/amend/reopen/interject.")
    return parser


def _run_dir(root: str, run_id: str) -> Path:
    return resolve_runs_root(root) / run_id


def _require_existing_run(root: str, run_id: str) -> Path | None:
    """§D0b: status/approve/amend/resume must error against a run that
    doesn't exist rather than let ``create_run_dir`` conjure an empty one —
    printing the resolved absolute path is the point: every reported
    instance of the "wrong directory" bug was invisible precisely because
    the path was never shown. ``run --run-id`` is exempt; it is allowed to
    create."""
    run_dir = _run_dir(root, run_id)
    if not (run_dir / "events.jsonl").is_file():
        print(f"no run at {run_dir} (missing events.jsonl)", file=sys.stderr)
        return None
    return run_dir


def cmd_run(argv: argparse.Namespace) -> int:
    if argv.detach:
        return cmd_run_detach(argv)
    from .run import run_from_args

    return run_from_args(_run_argv(argv, run_id=argv.run_id))


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


def _expand_source_arg(raw: str) -> str:
    """The corpus text behind a --source value: for ``@path``/``-`` read
    the file/stdin; anything else is inline text. Same rules as
    run.py:_read_text_arg (mirrored here because --detach must resolve the
    corpus *before* spawning its own run.py subprocess)."""
    if raw.startswith("@"):
        path = Path(raw[1:])
        if not path.exists():
            raise FileNotFoundError(f"source file not found: {path}")
        return path.read_text(encoding="utf-8", errors="replace").strip()
    if raw == "-":
        return sys.stdin.read().strip()
    return raw


def cmd_run_detach(argv: argparse.Namespace) -> int:
    run_id = argv.run_id or _default_run_id()
    workspace = getattr(argv, "workspace", None)
    # Same default --workspace -> runs_root rule as run.py's own
    # run_from_args (kept in sync here because --detach resolves the run
    # dir itself, below, before run.py ever parses its own argv).
    runs_root_arg = argv.runs_root or (
        f"{workspace.rstrip('/')}/.kusudaemon/runs" if workspace else _RUNS_ROOT_DEFAULT
    )
    # §11.10.8: the corpus must not ride through argv — an inline (non-@file)
    # corpus hits E2BIG well before "corpus-scale". Write it into the run
    # dir once (source.txt is exactly where driver._write_source_and_spec
    # would put it) and pass @path, which run.py then reads instead of argv.
    source_arg = argv.source
    if source_arg and not source_arg.startswith("@"):
        run_dir = _run_dir(runs_root_arg, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        resolved = _expand_source_arg(source_arg)
        source_path(run_dir).write_text(resolved, encoding="utf-8")
        source_arg = f"@{source_path(run_dir)}"
    command = [
        sys.executable,
        "-m",
        "kusudaemon.pipeline.run",
        "--runs-root", runs_root_arg,
        "--run-id", run_id,
        "--goal", argv.goal,
        "--source", source_arg,
        "--backend", argv.backend,
        "--max-rounds", str(argv.max_rounds),
        "--max-attempts", str(argv.max_attempts),
        "--dispatch-policy", argv.dispatch_policy,
        "--max-parallel", str(getattr(argv, "max_parallel", 1)),
    ]
    if workspace:
        command += ["--workspace", workspace]
    if getattr(argv, "tier", None):
        command += ["--tier", argv.tier]
    if argv.document_review:
        command += ["--document-review"]
    if argv.survey_mode != "model":
        command += ["--survey-mode", argv.survey_mode]
    if argv.inline_spans:
        command += ["--inline-spans"]
    for flag, value in (
        ("--model", argv.model),
        ("--compile-command", argv.compile_command),
        ("--research-plan", argv.research_plan),
    ):
        if value:
            command += [flag, value]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    print(f"detached pipeline run: run_id={run_id}")
    print(f"watch with: kusudaemon pipeline status {run_id} --runs-root {runs_root_arg}")
    return 0


def cmd_status(argv: argparse.Namespace) -> int:
    run_dir = _require_existing_run(argv.runs_root, argv.run_id)
    if run_dir is None:
        return 1
    import json

    phase: dict = {}
    try:
        phase = json.loads(phase_path(run_dir).read_text(encoding="utf-8"))
    except OSError:
        pass
    print(f"run:      {argv.run_id}")
    print(f"dir:      {run_dir}")
    print(f"phase:    {phase.get('phase', '-')} ({phase.get('status', '-')})")
    if phase.get("detail"):
        print(f"detail:   {phase['detail']}")
    liveness = run_liveness(run_dir)
    if liveness.stalled:
        print(f"STALLED:  {liveness.reason}")
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
    run_dir = _require_existing_run(argv.runs_root, argv.run_id)
    if run_dir is None:
        return 1
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
    # §11.10.4: the estimate is shown BEFORE the Reviewer pass runs — the
    # §10 approval was theater when the tokens were already spent.
    run_dir = _require_existing_run(argv.runs_root, argv.run_id)
    if run_dir is None:
        return 1
    provider = OpenAICompatibleProvider()
    options = _load_options(run_dir)
    phase1 = amend_contract_and_estimate(
        run_dir, rule_text=argv.text, reason=argv.reason
    )
    estimate = phase1["estimate"]
    print(
        f"amended contract; revalidation would review "
        f"{estimate.get('nodes', 0)} nodes (~{estimate.get('tokens', 0)} tokens, "
        f"{estimate.get('skipped', 0)} pre-filtered)"
    )
    if not argv.yes:
        try:
            choice = input("run the revalidation review? [y/N] ").strip().lower()
        except EOFError:
            choice = "n"
    else:
        choice = "y"
    if choice not in ("y", "yes"):
        print("not running revalidation; the amended contract stands")
        return 0
    phase2 = run_amendment_revalidation(
        run_dir,
        contract_text=phase1["contract"],
        rule_text=argv.text,
        provider=provider,
    )
    counts = phase2["counts"]
    print(
        f"revalidation: clean={counts['clean']} patchable={counts['patchable']} "
        f"regenerate={counts['regenerate']}"
    )
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
            triage=phase2["triage"],
            writer_adapter_factory=_writer_factory(options, run_dir),
            env=env,
            provider=provider,
            max_attempts=argv.max_attempts,
        )
    )
    print(f"repaired: {', '.join(repaired) if repaired else '(none)'}")
    return 0


def cmd_escalate(argv: argparse.Namespace) -> int:
    run_dir = _require_existing_run(argv.runs_root, argv.run_id)
    if run_dir is None:
        return 1
    from .driver import escalate_run

    result = escalate_run(run_dir)
    print(f"tier: {result['from']} -> {result['to']}")
    return 0


def cmd_resume(argv: argparse.Namespace) -> int:
    # Resume is exactly "run with an existing run-id": the driver converges
    # from durable state (§10), so there is no separate resume code path —
    # argv contributes nothing but the run id. But unlike `run --run-id`,
    # resume must never *create* a run: an id typo or a stale --runs-root
    # would otherwise silently start a fresh, empty run (§D0b).
    if _require_existing_run(argv.runs_root, argv.run_id) is None:
        return 1
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
    if command == "escalate":
        return cmd_escalate(args)
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
        "--goal", argv.goal,
        "--source", argv.source,
        "--backend", argv.backend,
        "--max-rounds", str(argv.max_rounds),
        "--max-attempts", str(argv.max_attempts),
        "--dispatch-policy", argv.dispatch_policy,
        "--max-parallel", str(getattr(argv, "max_parallel", 1)),
    ]
    if run_id:
        parts += ["--run-id", run_id]
    if getattr(argv, "runs_root", None):
        parts += ["--runs-root", argv.runs_root]
    if getattr(argv, "workspace", None):
        parts += ["--workspace", argv.workspace]
    if getattr(argv, "tier", None):
        parts += ["--tier", argv.tier]
    if getattr(argv, "document_review", False):
        parts += ["--document-review"]
    if getattr(argv, "survey_mode", "model") != "model":
        parts += ["--survey-mode", argv.survey_mode]
    if getattr(argv, "inline_spans", False):
        parts += ["--inline-spans"]
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