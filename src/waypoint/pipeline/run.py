"""Standalone pipeline entry point (``python -m waypoint.pipeline.run``).

Both the ``waypoint pipeline run`` command and its ``--detach`` mode
spawn exactly this module with the same arguments, so one argument parser
stays in sync with one run loop. Parsing is intentionally minimal: the
interactive surface (``status`` / ``approve`` / ``amend``) lives in the
``waypoint pipeline`` group, not here.
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
from .run_dir import run_spec_path

_RUNS_ROOT_DEFAULT = "./.waypoint/runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="waypoint pipeline run",
        description="Run the recursive decomposition pipeline (intake -> survey -> plan -> pilot -> research -> execute -> assemble).",
    )
    parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT, help="Base directory holding one isolated subfolder per run.")
    parser.add_argument("--run-id", default=None, help="Run id; defaults to a timestamp + uuid. Reusing one resumes it.")
    parser.add_argument("--goal", default="", help="Task goal (or @path).")
    parser.add_argument("--source", default="", help="Source document text, @file path, or - for stdin.")
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
    return parser


def _read_text_arg(raw: str | None) -> str:
    if not raw:
        return ""
    if raw.startswith("@"):
        return Path(raw[1:]).read_text(encoding="utf-8").strip()
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
    run_dir = Path(args.runs_root).expanduser() / run_id

    spec = run_spec_path(run_dir)
    if spec.exists():
        # Resume: the disk is authoritative — argv re-supplies nothing but
        # the run id (§10: durable state wins over the caller's memory).
        options = RunOptions.from_spec(json.loads(spec.read_text(encoding="utf-8")))
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
            compile_command=args.compile_command,
            research_plan=_parse_plan(args.research_plan),
            max_rounds=args.max_rounds,
            max_attempts=args.max_attempts,
        )

    driver = RecursiveDriver(
        run_dir,
        provider=OpenAICompatibleProvider(model=args.model),
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