"""Import smoke: every top-level module must import without side effects.

If any of these fail, OSS users hit a wall at `pip install -e .`.
"""
from __future__ import annotations

import importlib

import pytest


PUBLIC_MODULES = [
    "weavebench",
    "weavebench.eval.run_weavebench",
    "weavebench.eval.orchestrator",
    "weavebench.eval.judge_helper",
    "weavebench.eval.agent_judge.run_one_aj",
    "weavebench.eval.agent_judge.stage_case",
    "weavebench.eval.agent_judge.judge_runner",
    "weavebench.eval._runners.responses_gui",
    "weavebench.eval._runners.messages_gui",
    "weavebench.eval._runners.chat_gui",
    "weavebench.eval._runners.responses_cli",
    "weavebench.eval._runners.messages_cli",
    "weavebench.eval._runners.chat_cli",
    "weavebench.agents.openclaw_agent",
    "weavebench.agents.codex_agent",
    "weavebench.agents.claudecode_agent",
    "weavebench.agents.hermes_agent",
    "weavebench.scripts.quickstart",
    "weavebench.scripts.download_dataset",
    "weavebench.scripts.download_assets",
    "weavebench.utils.task_parser",
    "weavebench.utils.grading",
    "weavebench.utils.docker_utils",
]


@pytest.mark.parametrize("mod", PUBLIC_MODULES)
def test_import(mod):
    importlib.import_module(mod)
