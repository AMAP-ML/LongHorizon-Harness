"""Standalone pipeline entry point (``python -m kusudaemon.pipeline.run``).

Both the ``kusudaemon pipeline run`` command and its ``--detach`` mode
spawn exactly this module with the same arguments, so one argument parser
stays in sync with one run loop. Parsing is intentionally minimal: the
interactive surface (``status`` / ``approve`` / ``amend``) lives in the
``kusudaemon pipeline`` group, not here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

from ..environment.base import Environment
from ..v0.events import EventLog
from ..v1.provider import OpenAICompatibleProvider, RATE_LIMIT_BACKOFFS
from .backends import parse_research_plan
from .driver import RunOptions, RecursiveDriver
from .run_dir import resolve_runs_root, run_spec_path, events_path, halt_path

_RUNS_ROOT_DEFAULT = "~/.kusudaemon/runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kusudaemon pipeline run",
        description="Run the recursive decomposition pipeline (intake -> survey -> plan -> pilot -> research -> execute -> assemble).",
    )
    parser.add_argument(
        "--runs-root",
        default=None,
        help=f"Base directory holding one isolated subfolder per run. "
        f"Defaults to {_RUNS_ROOT_DEFAULT} (a run is harness-owned state, "
        "never stored inside the workspace it edits).",
    )
    parser.add_argument("--run-id", default=None, help="Run id; defaults to a timestamp + uuid. Reusing one resumes it.")
    parser.add_argument("--goal", default="", help="Task goal (or @path).")
    parser.add_argument("--source", default="", help="Source document text, @file path, or - for stdin.")
    parser.add_argument(
        "--workspace",
        default=None,
        help="Path to a real directory (a repo, a folder of notes) the "
        "Writer edits directly instead of a materialized corpus (PLAN.md "
        "§A3 kind=\"workspace\"). Measured once, deterministically, at "
        "startup (v6/work_object.py:measure_workspace) — no model call. "
        "Mutually exclusive in spirit with --source, though both may be "
        "set; only one kind flows into the run.",
    )
    parser.add_argument(
        "--backend",
        default="gptme",
        choices=("gptme", "claude", "codex", "opencode"),
        help="Subagent backend: gptme (default), claude (Claude Code CLI), codex (Codex CLI), opencode (OpenCode CLI). The CLI backends use their own auth, never the harness provider.",
    )
    parser.add_argument("--model", default=None, help="Provider model; defaults to the provider config (default: opencode's DeepSeek V4 Flash Free).")
    parser.add_argument(
        "--provider",
        default=None,
        help="Named provider from provider.json whose base_url and key the "
        "direct-call provider uses; defaults to the config's default (or is "
        "inferred from --model).",
    )
    parser.add_argument("--compile-command", default=None, help="Optional shell command run as the assembly compile gate (e.g. 'latexmk -pdf').")
    parser.add_argument(
        "--research-plan",
        default=None,
        help="JSON research plan: a list of {node_id, slug, kind, question} objects, or a dict mapping node_id to that list. Prefix with @ for a file path.",
    )
    parser.add_argument("--max-rounds", type=int, default=100, help="Maximum orchestrator rounds in the execute phase.")
    parser.add_argument("--max-attempts", type=int, default=3, help="Retry limit per node before it is blocked.")
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=1,
        help="Writer episodes dispatched concurrently per round (PLAN.md §C2). "
        "1 is the exact legacy serial behavior; raise only for providers and "
        "workspaces that tolerate concurrency.",
    )
    parser.add_argument(
        "--dispatch-policy",
        choices=("model", "document_order"),
        default="model",
        help="document_order skips the per-round orchestrator LLM call and "
        "dispatches the earliest ready node in document order (PLAN-zeromem.md §1)",
    )
    parser.add_argument(
        "--document-review",
        action="store_true",
        help="Run PLAN-zeromem.md §8 document-level review passes after "
        "assembly (approval-gated before repairs).",
    )
    parser.add_argument(
        "--survey-mode",
        choices=("model", "embedding"),
        default="model",
        help="embedding replaces the per-window model survey with "
        "embedding-dissimilarity boundaries (PLAN-zeromem.md §3); "
        "falls back to model when kusudaemon[retrieval] is not installed.",
    )
    parser.add_argument(
        "--inline-spans",
        action="store_true",
        help="Inline top-k retrieved spans from the node's own spine slice "
        "into its prompt instead of bare input paths (PLAN-zeromem.md §4).",
    )
    parser.add_argument(
        "--tier",
        choices=("T0", "T1", "T2", "T3"),
        default=None,
        help="Force a tier floor, never a ceiling (PLAN.md §A4.4 invariant "
        "9): the classifier still runs; the effective tier is "
        "max(measured, this).",
    )
    parser.add_argument(
        "--no-intake",
        action="store_true",
        help="Skip intake entirely (no clarifying questions, no objection "
        "surfacing; spec.md gets the minimal rendering). With this set, the "
        "classify phase also skips its estimate call when the free size "
        "signals already force >= T2 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md "
        "A5-1).",
    )
    return parser


def _read_text_arg(raw: str | None) -> str:
    if not raw:
        return ""
    if raw.startswith("@"):
        path = Path(raw[1:])
        if path.suffix.lower() == ".pdf":
            try:
                import pypdf
                reader = pypdf.PdfReader(path)
                return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
            except ImportError:
                pass
        try:
            return path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError:
            return path.read_text(encoding="utf-8", errors="replace").strip()
    if raw == "-":
        return sys.stdin.read().strip()
    return raw


def _parse_plan(raw: str | None) -> dict:
    text = _read_text_arg(raw)
    if not text.strip():
        return {}
    return parse_research_plan(json.loads(text))


def _log_rate_limit_backoff_for(run_dir: Path) -> Callable[[int, float], None]:
    """§D11: return the ``on_backoff`` callback the provider fires before
    each rung of the rate-limit ladder sleeps. Appends one
    ``rate_limit_backoff`` line to ``events.jsonl`` so an operator watching
    the dashboard sees *why* a phase is sitting mid-call for up to five
    hours, instead of a silent ``in_progress``. The ``EventLog`` is built
    lazily on first fire (most runs never hit a 429, so never pay for it);
    once built it's reused across the run's retries."""
    log: list[EventLog] = []  # one-slot cache so the EventLog is created at most once

    def _on_backoff(attempt: int, delay_s: float) -> None:
        if not log:
            log.append(EventLog(events_path(run_dir)))
        log[0].append(
            {
                "node_id": "-",
                "role": "harness",
                "round": 0,
                "type": "rate_limit_backoff",
                "attempt": attempt,
                "delay_s": delay_s,
                "rungs": len(RATE_LIMIT_BACKOFFS),
            }
        )

    return _on_backoff


def run_from_args(argv: list[str] | None = None, *, env: Environment | None = None) -> int:
    from ..provider_config import load_env_file

    load_env_file()
    args = build_parser().parse_args(argv)
    run_id = args.run_id or f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:8]}"
    # Runs live under ~/.kusudaemon/runs by default — for --workspace mode
    # too (the run dir is harness-owned bookkeeping, and keeping it out of
    # the repo means a workspace scan never sees it and a repo can't grow a
    # hidden state folder). An explicit --runs-root still wins.
    runs_root_arg = args.runs_root or _RUNS_ROOT_DEFAULT
    # §D0b: resolve once, here, against the *root* — resolving only the
    # run_dir/spec join (as the driver's own .resolve() does) still leaves
    # a relative runs_root anchored to whatever cwd this process happened
    # to be launched from.
    run_dir = resolve_runs_root(runs_root_arg) / run_id
    print(f"run dir: {run_dir}")

    work_object = None
    if args.workspace:
        from ..v6.work_object import measure_workspace

        work_object = measure_workspace(args.workspace)

    spec = run_spec_path(run_dir)
    if spec.exists():
        # Resume: disk is authoritative — argv re-supplies nothing but
        # the run id (§10: durable state wins over the caller's memory).
        # RunOptions.from_spec re-hydrates workspace_root and measures work_object.
        # If --workspace is explicitly passed, it re-points workspace_root.
        options = RunOptions.from_spec(json.loads(spec.read_text(encoding="utf-8")))
        if work_object is not None:
            options.work_object = work_object
            options.workspace_root = str(Path(args.workspace).expanduser().resolve())
        try:
            halt_path(run_dir).unlink()
        except OSError:
            pass
    else:
        goal = _read_text_arg(args.goal).strip()
        if not goal:
            print("--goal is required for a new run", file=sys.stderr)
            return 2
        options = RunOptions(
            goal=goal,
            backend=args.backend,
            model=args.model,
            provider=args.provider,
            source_text=_read_text_arg(args.source),
            work_object=work_object,
            compile_command=args.compile_command,
            research_plan=_parse_plan(args.research_plan),
            max_rounds=args.max_rounds,
            max_attempts=args.max_attempts,
            dispatch_policy=args.dispatch_policy,
            max_parallel=args.max_parallel,
            document_review=args.document_review,
            survey_mode=args.survey_mode,
            inline_spans=args.inline_spans,
            tier_override=args.tier,
            no_intake=args.no_intake,
        )

    driver = RecursiveDriver(
        run_dir,
        # §11.9: on a bare `resume <id>` argv supplies no --model; the
        # provider must honor the model recorded in run.spec.json, not
        # silently fall back to the config default mid-run.
        # §D11: ``on_backoff`` wires the rate-limit ladder's per-wait
        # observability into ``events.jsonl`` — a phase stuck mid-call for
        # up to five hours otherwise shows as a silent ``in_progress`` with
        # nothing explaining it. (The run dir already exists by this point:
        # ``create_run_dir`` runs in ``RecursiveDriver.__init__`` before
        # any provider call happens.)
        provider=OpenAICompatibleProvider(
            model=options.model,
            provider=options.provider,
            on_backoff=_log_rate_limit_backoff_for(run_dir),
        ),
        options=options,
        env=env,
    )
    report = asyncio.run(driver.run())
    print(f"pipeline: status={report.status} phase={report.phase} tree={report.tree_counts}")
    if report.detail:
        print(f"detail: {report.detail}")
    return 0 if report.status in ("done", "halted") else 1


if __name__ == "__main__":
    raise SystemExit(run_from_args())