from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from lh_harness.adapters.claude_code import ClaudeCodeAdapter
from lh_harness.adapters.codex import CodexAdapter
from lh_harness.adapters.deepseek_harness import DeepSeekHarnessAdapter
from lh_harness.adapters.opencode import OpenCodeAdapter
from lh_harness.cli import _resolve_role_reasoning_effort
from lh_harness.config import ProjectConfigError, load_run_defaults
from lh_harness.supervisor.service import _normalise_role_configs


def _codex(**kwargs) -> CodexAdapter:
    return CodexAdapter(workspace_path="/tmp/ws", prompt_dir="/tmp/prompts", **kwargs)


def test_codex_sends_the_effort_as_a_config_override() -> None:
    # Codex has no `--effort` flag; the depth is a config value.
    adapter = _codex(model="gpt-5.6-sol", reasoning_effort="xhigh")

    tokens = adapter.argv

    assert 'model_reasoning_effort="xhigh"' in tokens
    assert tokens[tokens.index('model_reasoning_effort="xhigh"') - 1] == "-c"


def test_codex_without_an_effort_leaves_the_user_config_in_charge() -> None:
    adapter = _codex(model="gpt-5.6-sol")

    assert not any("model_reasoning_effort" in token for token in adapter.argv)


def test_claude_sends_the_effort_as_a_cli_flag() -> None:
    adapter = ClaudeCodeAdapter(
        model="claude-opus-5",
        role="manager",
        workspace_path="/tmp/ws",
        prompt_dir="/tmp/prompts",
        reasoning_effort="max",
    )

    tokens = adapter.argv

    assert tokens[tokens.index("--effort") + 1] == "max"
    assert adapter.reasoning_effort == "max"


def test_opencode_sends_the_effort_as_a_variant() -> None:
    adapter = OpenCodeAdapter(
        model="opencode/deepseek-v4-flash-free",
        workspace_path="/tmp/ws",
        prompt_dir="/tmp/prompts",
        reasoning_effort="high",
    )

    tokens = adapter.argv

    assert tokens[tokens.index("--variant") + 1] == "high"


def test_opencode_still_accepts_the_legacy_effort_keyword() -> None:
    adapter = OpenCodeAdapter(
        model="opencode/x",
        workspace_path="/tmp/ws",
        prompt_dir="/tmp/prompts",
        effort="minimal",
    )

    assert adapter.effort == "minimal"


@pytest.mark.parametrize("value", ['high"', "high'", "high high", "high\nlow", "x" * 65])
def test_adapters_reject_effort_values_that_could_break_the_command(value: str) -> None:
    # The value lands in Codex's inline TOML and in the others' argv.
    with pytest.raises(ValueError):
        _codex(model="gpt-5.6-sol", reasoning_effort=value)
    with pytest.raises(ValueError):
        OpenCodeAdapter(
            model="opencode/x", workspace_path="/tmp/ws", prompt_dir="/tmp/prompts", effort=value
        )


def test_deepseek_rejects_an_effort_instead_of_dropping_it() -> None:
    # Ignoring it would let the workbench claim a depth the run never applied.
    with pytest.raises(ValueError, match="does not accept a reasoning effort"):
        DeepSeekHarnessAdapter(role="cli_executor", reasoning_effort="high")


def _args(**values) -> argparse.Namespace:
    base = {"agent": None, "model": None, "reasoning_effort": None}
    for role in (
        "manager",
        "executor",
        "gui_executor",
        "cli_executor",
        "auditor",
        "gui_auditor",
        "cli_auditor",
        "final_response",
    ):
        base[f"{role}_agent"] = None
        base[f"{role}_model"] = None
        base[f"{role}_reasoning_effort"] = None
    base.update(values)
    return argparse.Namespace(**base)


def test_role_effort_falls_back_to_the_global_value() -> None:
    args = _args(reasoning_effort="high")

    assert _resolve_role_reasoning_effort(args, "gui_executor", "codex") == "high"


def test_role_effort_prefers_the_nearest_override() -> None:
    args = _args(reasoning_effort="low", executor_reasoning_effort="max")

    assert _resolve_role_reasoning_effort(args, "gui_executor", "codex") == "max"
    assert _resolve_role_reasoning_effort(args, "manager", "codex") == "low"


def test_role_effort_does_not_cross_an_explicit_backend_switch() -> None:
    # Effort tiers are backend-specific: Codex accepts `ultra`, Claude Code does
    # not, so inheriting across a switch would send a rejected or ignored tier.
    args = _args(reasoning_effort="ultra", manager_agent="claude_code")

    assert _resolve_role_reasoning_effort(args, "manager", "claude_code") is None
    assert _resolve_role_reasoning_effort(args, "gui_executor", "codex") == "ultra"


def test_role_effort_is_dropped_for_a_backend_without_the_switch() -> None:
    args = _args(reasoning_effort="high")

    assert _resolve_role_reasoning_effort(args, "manager", "deepseek_harness") is None


def test_supervisor_role_configs_keep_a_per_role_effort() -> None:
    resolved = _normalise_role_configs(
        {"manager": {"reasoning_effort": "xhigh"}, "executor": {}, "auditor": {}},
        agent="codex",
        model="gpt-5.6-sol",
    )

    assert resolved["manager"]["reasoning_effort"] == "xhigh"
    assert "reasoning_effort" not in resolved["executor"]


def test_supervisor_global_effort_only_reaches_roles_on_the_same_backend() -> None:
    resolved = _normalise_role_configs(
        {"manager": {"agent": "deepseek_harness"}, "executor": {}, "auditor": {}},
        agent="codex",
        model="gpt-5.6-sol",
        reasoning_effort="high",
    )

    # DeepSeek has no effort switch, and it kept none of the global value.
    assert "reasoning_effort" not in resolved["manager"]
    assert resolved["executor"]["reasoning_effort"] == "high"
    assert resolved["auditor"]["reasoning_effort"] == "high"


def test_supervisor_rejects_an_effort_for_a_backend_without_the_switch() -> None:
    with pytest.raises(ValueError, match="does not accept a reasoning effort"):
        _normalise_role_configs(
            {"manager": {"agent": "deepseek_harness", "reasoning_effort": "high"}},
            agent="codex",
            model="gpt-5.6-sol",
        )


def test_supervisor_rejects_a_malformed_role_effort() -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        _normalise_role_configs(
            {"manager": {"reasoning_effort": 'a"b'}},
            agent="codex",
            model="gpt-5.6-sol",
        )


def test_worker_command_forwards_the_role_effort(tmp_path: Path) -> None:
    from lh_harness.supervisor.service import RunSupervisor

    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path)
    command = supervisor._worker_command(
        run_id="run-1",
        task="@/tmp/task.md",
        agent="codex",
        model="gpt-5.6-sol",
        role_configs={
            "manager": {"agent": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
            "executor": {"agent": "codex", "model": "gpt-5.6-sol"},
            "auditor": {"agent": "codex", "model": "gpt-5.6-sol"},
        },
        workspace=str(tmp_path),
        max_rounds=5,
        prompt_language="en",
    )

    assert "--manager-reasoning-effort=xhigh" in command
    assert not any(item.startswith("--executor-reasoning-effort") for item in command)


def test_worker_command_forwards_a_global_effort_without_role_configs(tmp_path: Path) -> None:
    from lh_harness.supervisor.service import RunSupervisor

    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path)
    command = supervisor._worker_command(
        run_id="run-1",
        task="@/tmp/task.md",
        agent="codex",
        model=None,
        role_configs=None,
        workspace=str(tmp_path),
        max_rounds=5,
        prompt_language="en",
        reasoning_effort="high",
    )

    assert "--reasoning-effort=high" in command


def test_project_config_accepts_a_global_and_per_role_effort(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            [
                "[run]",
                'reasoning_effort = "high"',
                "[run.roles.manager]",
                'reasoning_effort = "xhigh"',
            ]
        ),
        encoding="utf-8",
    )

    defaults = load_run_defaults(config)

    assert defaults["reasoning_effort"] == "high"
    assert defaults["manager_reasoning_effort"] == "xhigh"


def test_project_config_rejects_a_malformed_effort(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[run]\nreasoning_effort = "a b"\n', encoding="utf-8")

    with pytest.raises(ProjectConfigError, match="run.reasoning_effort"):
        load_run_defaults(config)
