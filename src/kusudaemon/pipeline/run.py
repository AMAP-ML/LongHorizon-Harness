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

from ..environment.base import Environment
from ..v1.provider import OpenAICompatibleProvider
from .backends import parse_research_plan
from .driver import RunOptions, RecursiveDriver
from .run_dir import resolve_runs_root, run_spec_path

_RUNS_ROOT_DEFAULT = "./.kusudaemon/runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kusudaemon pipeline run",
        description="Run the recursive decomposition pipeline (intake -> survey -> plan -> pilot -> research -> execute -> assemble).",
    )
    parser.add_argument(
        "--runs-root",
        default=None,
        help=f"Base directory holding one isolated subfolder per run. "
        f"Defaults to {_RUNS_ROOT_DEFAULT}, or <workspace>/.kusudaemon/runs "
        "when --workspace is given (PLAN.md §A3 point 1: the run dir stays "
        "inside the project it was launched from).",
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
    parser.add_argument("--backend", default="gptme", choices=("gptme",), help="Writer agent backend (only gptme in this harness).")
    parser.add_argument("--model", default=None, help="Provider model; defaults to the provider config (default: opencode's DeepSeek V4 Flash Free).")
    parser.add_argument("--compile-command", default=None, help="Optional shell command run as the assembly compile gate (e.g. 'latexmk -pdf').")
    parser.add_argument(
        "--research-plan",
        default=None,
        help="JSON research plan: a list of {node_id, slug, kind, question} objects, or a dict mapping node_id to that list. Prefix with @ for a file path.",
    )
    parser.add_argument("--max-rounds", type=int, default=100, help="Maximum orchestrator rounds in the execute phase.")
    parser.add_argument("--max-attempts", type=int, default=3, help="Retry limit per node before it is blocked.")
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


def run_from_args(argv: list[str] | None = None, *, env: Environment | None = None) -> int:
    from ..provider_config import load_env_file

    load_env_file()
    args = build_parser().parse_args(argv)
    run_id = args.run_id or f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:8]}"
    # PLAN.md §A3 point 1: --workspace's own default for --runs-root, so a
    # workspace-mode run's bookkeeping lands inside the project it was
    # launched from (types.py's existing repo-local rule) without the
    # caller having to spell out --runs-root every time. An explicit
    # --runs-root still wins either way.
    runs_root_arg = args.runs_root or (
        f"{args.workspace.rstrip('/')}/.kusudaemon/runs" if args.workspace else _RUNS_ROOT_DEFAULT
    )
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
        # Resume: the disk is authoritative — argv re-supplies nothing but
        # the run id (§10: durable state wins over the caller's memory).
        # work_object is the one exception: like source_text, it's a
        # constructor input rather than a persisted spec field (comment on
        # RunOptions.work_object) — a resumed workspace-mode run needs
        # --workspace re-supplied, exactly as measured above, to pick its
        # WorkObject back up.
        options = RunOptions.from_spec(json.loads(spec.read_text(encoding="utf-8")))
        options.work_object = work_object
    else:
        goal = _read_text_arg(args.goal).strip()
        if not goal:
            print("--goal is required for a new run", file=sys.stderr)
            return 2
        options = RunOptions(
            goal=goal,
            backend=args.backend,
            model=args.model,
            source_text=_read_text_arg(args.source),
            work_object=work_object,
            compile_command=args.compile_command,
            research_plan=_parse_plan(args.research_plan),
            max_rounds=args.max_rounds,
            max_attempts=args.max_attempts,
            dispatch_policy=args.dispatch_policy,
            document_review=args.document_review,
            survey_mode=args.survey_mode,
            inline_spans=args.inline_spans,
            tier_override=args.tier,
        )

    driver = RecursiveDriver(
        run_dir,
        # §11.9: on a bare `resume <id>` argv supplies no --model; the
        # provider must honor the model recorded in run.spec.json, not
        # silently fall back to the config default mid-run.
        provider=OpenAICompatibleProvider(model=options.model),
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