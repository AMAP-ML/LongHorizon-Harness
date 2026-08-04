"""Regression tests for the weavebench-run -> internal-runner argv contract.

The bug this catches: `run_weavebench.main` forwards `--mode cli` to the
chosen runner, but the cli runners previously did not declare `--mode`
in their parsers. Python argparse with `allow_abbrev=True` (default)
treats `--mode` as a unique prefix of `--model`, so the cli runner
silently parsed `args.model = "cli"`. The model id passed by the user
was discarded.

Fix is two-fold: (a) cli runners now declare `--mode`, (b) all six
runners pass `allow_abbrev=False` so future flag collisions also fail
loudly instead of silently mis-parsing.
"""
from __future__ import annotations

import importlib
import sys

import pytest


# All six runners exercised by weavebench-run dispatch
RUNNERS = [
    "weavebench.eval._runners.chat_cli",
    "weavebench.eval._runners.messages_cli",
    "weavebench.eval._runners.responses_cli",
    "weavebench.eval._runners.chat_gui",
    "weavebench.eval._runners.messages_gui",
    "weavebench.eval._runners.responses_gui",
]


# What run_weavebench.main forwards (see run_weavebench.py:168-178)
FORWARDED_ARGV = [
    "--model", "openai/gpt-5",
    "--bench_subdirs", ".",
    "--categories", "WEB",
    "--num_envs", "1",
    "--max_steps", "50",
    "--mode", "cli",
    "--result_dir", "/tmp/weavebench_argv_test",
    "--tasks_root", "/tmp/fake_tasks",
]


@pytest.mark.parametrize("runner_mod", RUNNERS)
def test_runner_preserves_model_id_with_mode_flag(monkeypatch, runner_mod):
    """The model id must survive the --mode cli forwarding (allow_abbrev=False
    + explicit --mode in cli runners). If it doesn't, args.model becomes 'cli'
    and the runner tries to invoke a non-existent OpenRouter model."""
    mod = importlib.import_module(runner_mod)
    assert hasattr(mod, "parse_args"), f"{runner_mod} has no parse_args()"

    # Simulate the runner being invoked via weavebench-run dispatch
    monkeypatch.setattr(sys, "argv", [runner_mod] + FORWARDED_ARGV)
    args = mod.parse_args()

    assert args.model == "openai/gpt-5", (
        f"{runner_mod}: model id corrupted to {args.model!r} — "
        f"likely --mode being abbreviated as --model"
    )
    assert args.mode == "cli", (
        f"{runner_mod}: --mode not honored, got {args.mode!r}"
    )
    assert args.result_dir == "/tmp/weavebench_argv_test"
