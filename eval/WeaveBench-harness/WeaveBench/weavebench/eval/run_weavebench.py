"""WeaveBench top-level CLI entrypoint (`weavebench-run`).

Thin wrapper around the per-transport runners in this package. The user
selects a harness + LLM model + task filter; we translate that into the
right runner module and re-exec it.

This file MUST be kept small — all real logic stays in:
    - weavebench.eval.orchestrator (discover_tasks, run_one, run_grading)
    - weavebench.eval._runners.responses_gui{,_messages,_chat} (the actual runners)

Examples
--------
Run one task on OpenClaw with GPT-5.5 via OpenRouter::

    export OPENROUTER_API_KEY=sk-or-v1-...
    weavebench-run \
        --harness openclaw \
        --model openai/gpt-5.5 \
        --tasks_root ./cache/tasks \
        --domains WEB \
        --task_filter task_1_ \
        --result_dir ./results/smoke

Run the full 114-task sweep on Codex CLI with GPT-5.5::

    weavebench-run \
        --harness codex \
        --model openai/gpt-5.5 \
        --tasks_root ./cache/tasks \
        --num_envs 8 \
        --result_dir ./results/codex_gpt55
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


# Map (harness, transport, mode) -> internal runner module.
# All runners monkey-patch ds.run_one -> run_one_aj at import time, so
# the trajectory-aware agent-as-judge is unconditionally the scoring path.
_RUNNER_DISPATCH = {
    # GUI runners (full DesktopEnv inside a KVM/qcow2 VM)
    ("openclaw", "responses", "gui"): "weavebench.eval._runners.responses_gui",
    ("openclaw", "messages", "gui"): "weavebench.eval._runners.messages_gui",
    ("openclaw", "chat", "gui"): "weavebench.eval._runners.chat_gui",
    ("codex", None, "gui"): "weavebench.eval._runners.responses_gui",
    ("claudecode", None, "gui"): "weavebench.eval._runners.messages_gui",
    ("cua_harness", None, "gui"): "weavebench.eval._runners.messages_gui",
    ("cua_harness_claudecode", None, "gui"): "weavebench.eval._runners.messages_gui",
    ("hermes", None, "gui"): "weavebench.eval._runners.responses_gui",
    # CLI runners (headless docker_lite, no X server) — openclaw only
    ("openclaw", "responses", "cli"): "weavebench.eval._runners.responses_cli",
    ("openclaw", "messages", "cli"): "weavebench.eval._runners.messages_cli",
    ("openclaw", "chat", "cli"): "weavebench.eval._runners.chat_cli",
}


def _resolve_transport(harness: str, transport: str, model: str) -> str | None:
    """Materialize the 'auto' transport pseudo-value into a real choice."""
    if harness != "openclaw":
        return None  # codex/claudecode/hermes pick their own internally
    if transport != "auto":
        return transport
    # 'auto': pick the lossless API for the model family, chat as fallback.
    m = (model or "").lower()
    if m.startswith("anthropic/") or "claude" in m:
        return "messages"  # preserves Claude thinking blocks + tool_use signatures
    return "chat"  # universal OpenRouter path


def _resolve_runner(harness: str, transport: str | None, mode: str) -> str:
    if mode == "both":
        # "both" walks gui+cli; route through GUI runner which can spin either.
        mode = "gui"
    if harness in {"cua_harness", "cua_harness_claudecode"} and mode == "cli":
        raise SystemExit(
            f"[weavebench] {harness} does not support --mode cli; use --mode gui "
            "so CUA verifier roles can audit desktop evidence."
        )
    key = (harness, transport if harness == "openclaw" else None, mode)
    runner = _RUNNER_DISPATCH.get(key)
    if runner is None:
        raise SystemExit(
            f"[weavebench] unsupported harness/transport/mode: {key!r}. "
            f"CLI mode is openclaw-only. Supported: {sorted(_RUNNER_DISPATCH)}"
        )
    return runner


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(allow_abbrev=False, 
        prog="weavebench-run",
        description="Run a WeaveBench evaluation across one or more harnesses.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--harness",
        choices=["openclaw", "codex", "claudecode", "cua_harness", "cua_harness_claudecode", "hermes"],
        required=True,
        help="Which agent harness to run inside the VM.",
    )
    ap.add_argument(
        "--transport",
        choices=["responses", "messages", "chat", "auto"],
        default="auto",
        help="LLM transport for openclaw (ignored by codex/claudecode/cua_harness/hermes). "
             "'auto' picks chat for everything except anthropic/* models, "
             "which get messages so Claude's thinking blocks survive. Use chat "
             "explicitly to force the universal OpenRouter path.",
    )
    ap.add_argument(
        "--model",
        required=True,
        help="Model id forwarded to OpenRouter, e.g. 'openai/gpt-5.5' "
             "or 'anthropic/claude-opus-4.7'.",
    )
    ap.add_argument(
        "--tasks_root",
        default=os.environ.get("WEAVEBENCH_TASKS_ROOT", "./cache/tasks"),
        help="Root dir containing the 8 domain folders (DAV/DES/DOC/DSK/"
             "GAM/OPS/SPA/WEB). Populate via `weavebench-download-dataset`.",
    )
    ap.add_argument(
        "--domains",
        default="DAV,DES,DOC,DSK,GAM,OPS,SPA,WEB",
        help="CSV of domains to run (default: all 8). Examples: 'WEB' for "
             "one domain, 'WEB,DAV' for two.",
    )
    ap.add_argument(
        "--task_filter",
        default=None,
        help="Substring filter on task ids, e.g. 'task_1' to run task_1_* "
             "from every selected domain. Default: all 114 tasks.",
    )

    # Pass-through args (forwarded verbatim to the underlying runner).
    passthrough = ap.add_argument_group("runner options")
    passthrough.add_argument("--num_envs", type=int, default=1,
                             help="Parallel VM workers (default 1).")
    passthrough.add_argument("--max_steps", type=int, default=300,
                             help="Max agent turns per task (default 300).")
    passthrough.add_argument("--mode", choices=["gui", "cli", "both"], default="gui",
                             help="gui (default, needs X server) or cli (headless docker_lite).")
    passthrough.add_argument("--result_dir", required=True)
    passthrough.add_argument("--limit", type=int, default=0,
                             help="Only run the first N discovered tasks (0 = no limit).")
    passthrough.add_argument("--litellm_base_url", default=None,
                             help="Override the LLM endpoint (defaults to OpenRouter or $OPENROUTER_BASE_URL).")
    passthrough.add_argument("--litellm_api_key", default=None,
                             help="Override the LLM API key (defaults to $OPENROUTER_API_KEY).")

    args = ap.parse_args(argv)

    api_key = args.litellm_api_key or os.environ.get("WEAVEBENCH_LITELLM_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit(
            "[weavebench] no API key. Set $OPENROUTER_API_KEY (or pass "
            "--litellm_api_key) — needed for both the agent and the judge."
        )

    transport = _resolve_transport(args.harness, args.transport, args.model)
    runner_module = _resolve_runner(args.harness, transport, args.mode)
    if args.harness == "openclaw" and args.transport == "auto":
        print(f"[weavebench] transport=auto -> {transport!r} (for model {args.model!r})", file=sys.stderr)

    # Build the argument vector the internal runner expects. The runners
    # still accept --bench_subdirs / --categories from the previous layout;
    # we map our user-facing --domains onto them and pass "." for
    # bench_subdir so they walk <tasks_root>/<DOMAIN>/*.md directly.
    forwarded_mode = (
        "gui"
        if args.harness in {"cua_harness", "cua_harness_claudecode"} and args.mode == "both"
        else args.mode
    )
    forwarded: list[str] = [
        "--model", args.model,
        "--bench_subdirs", ".",
        "--categories", args.domains,
        "--num_envs", str(args.num_envs),
        "--max_steps", str(args.max_steps),
        "--mode", forwarded_mode,
        "--result_dir", args.result_dir,
        "--tasks_root", str(Path(args.tasks_root).resolve()),
    ]
    if args.task_filter:
        forwarded += ["--task_filter", args.task_filter]
    if args.limit:
        forwarded += ["--limit", str(args.limit)]
    if args.litellm_base_url:
        forwarded += ["--litellm_base_url", args.litellm_base_url]
    if args.litellm_api_key:
        forwarded += ["--litellm_api_key", args.litellm_api_key]

    # Pass the harness through so the runner picks the right agent class.
    os.environ["WEAVEBENCH_HARNESS"] = args.harness

    # Re-dispatch into the chosen runner's main().
    import importlib
    mod = importlib.import_module(runner_module)
    if hasattr(mod, "main"):
        sys.argv = [runner_module] + forwarded
        return int(mod.main() or 0)
    # Fallback: call the script as `python -m runner ...`
    import runpy
    sys.argv = [runner_module] + forwarded
    runpy.run_module(runner_module, run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
