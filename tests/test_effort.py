"""Per-role effort: config keys, CLI fallback chain, verbatim backend dials.

Effort values pass through to each backend verbatim — no cross-backend
mapping.  Each backend accepts exactly the levels it documents and rejects
the rest, so an operator can never believe a run used a depth the backend
silently substituted.
"""

import argparse
import asyncio
import json
import stat
from pathlib import Path

import pytest

from lh_harness.adapters import opencode as opencode_adapter_module
from lh_harness.adapters.claude_code import ClaudeCodeAdapter
from lh_harness.adapters.codex import CodexAdapter
from lh_harness.adapters.deepseek_harness import DeepSeekHarnessAdapter
from lh_harness.adapters.deepseek_runner import run as deepseek_runner_run
from lh_harness.adapters.opencode import OpenCodeAdapter
from lh_harness.cli import _ROLE_PARENTS, _build_agent, _resolve_role_effort
from lh_harness.config import ProjectConfigError, _flatten_run_table
from lh_harness.environment.local import LocalEnvironment
from lh_harness.types import BACKEND_EFFORT_LEVELS, EpisodeBudget


# --- config ----------------------------------------------------------------


def test_config_accepts_global_effort() -> None:
    assert _flatten_run_table({"effort": "xhigh"}) == {"effort": "xhigh"}


def test_config_accepts_per_role_effort() -> None:
    defaults = _flatten_run_table(
        {
            "roles": {
                "manager": {"effort": "max"},
                "auditor": {"effort": "low"},
            }
        }
    )

    assert defaults["manager_effort"] == "max"
    assert defaults["auditor_effort"] == "low"


def test_config_accepts_custom_opencode_variant_names() -> None:
    # Effort names are backend-defined (OpenCode variants may be user-defined
    # in opencode.jsonc), so the config layer checks only the shape; the
    # role's own backend rejects names it does not support when the agent is
    # built, exactly like the web supervisor boundary.
    assert _flatten_run_table({"effort": "deep"}) == {"effort": "deep"}
    defaults = _flatten_run_table({"roles": {"executor": {"effort": "my-variant"}}})
    assert defaults["executor_effort"] == "my-variant"


@pytest.mark.parametrize("bad", ["", 3, True, ["high"], "x" * 65, "nul\x00led"])
def test_config_rejects_malformed_effort_values(bad: object) -> None:
    with pytest.raises(ProjectConfigError, match=r"\beffort\b"):
        _flatten_run_table({"effort": bad})
    with pytest.raises(ProjectConfigError, match=r"\beffort\b"):
        _flatten_run_table({"roles": {"executor": {"effort": bad}}})


# --- CLI fallback chain ----------------------------------------------------


def _args(**overrides: str) -> argparse.Namespace:
    ns = argparse.Namespace(effort=None)
    for role in _ROLE_PARENTS:
        setattr(ns, f"{role}_effort", None)
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def test_role_specific_effort_wins() -> None:
    args = _args(effort="low", gui_executor_effort="high")

    assert _resolve_role_effort(args, "gui_executor") == "high"


def test_effort_inherits_down_the_role_chain() -> None:
    args = _args(executor_effort="medium")

    assert _resolve_role_effort(args, "cli_executor") == "medium"
    assert _resolve_role_effort(args, "manager") is None


def test_effort_falls_back_to_the_global_flag() -> None:
    args = _args(effort="minimal")

    assert _resolve_role_effort(args, "auditor") == "minimal"


def test_effort_defaults_to_backend_default() -> None:
    assert _resolve_role_effort(_args(), "manager") is None


# --- Codex: -c model_reasoning_effort --------------------------------------


@pytest.mark.parametrize("level", BACKEND_EFFORT_LEVELS["codex"])
def test_codex_passes_its_levels_verbatim(tmp_path: Path, level: str) -> None:
    adapter = CodexAdapter(
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
        effort=level,
    )

    assert f'model_reasoning_effort="{level}"' in adapter.command_template
    assert adapter.effort == level


@pytest.mark.parametrize("level", ["max", "none", "ultra"])
def test_codex_rejects_levels_it_does_not_document(tmp_path: Path, level: str) -> None:
    with pytest.raises(ValueError, match="effort must be one of"):
        CodexAdapter(
            workspace_path=str(tmp_path),
            prompt_dir=str(tmp_path / "prompts"),
            effort=level,
        )


def test_codex_omits_the_override_without_effort(tmp_path: Path) -> None:
    adapter = CodexAdapter(
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
    )

    assert "model_reasoning_effort" not in adapter.command_template


# --- Claude Code: CLAUDE_CODE_EFFORT_LEVEL ----------------------------------


@pytest.mark.parametrize("level", BACKEND_EFFORT_LEVELS["claude_code"])
def test_claude_passes_its_levels_verbatim(tmp_path: Path, level: str) -> None:
    adapter = ClaudeCodeAdapter(
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
        effort=level,
    )

    assert f"CLAUDE_CODE_EFFORT_LEVEL={level}" in adapter.command_template
    assert adapter.effort == level


@pytest.mark.parametrize("level", ["minimal", "none", "ultra"])
def test_claude_rejects_levels_it_does_not_document(tmp_path: Path, level: str) -> None:
    with pytest.raises(ValueError, match="effort must be one of"):
        ClaudeCodeAdapter(
            workspace_path=str(tmp_path),
            prompt_dir=str(tmp_path / "prompts"),
            effort=level,
        )


def test_claude_omits_the_env_without_effort(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter(
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
    )

    assert "CLAUDE_CODE_EFFORT_LEVEL" not in adapter.command_template


# --- DeepSeek Harness: reasoningEffort profile patch ------------------------


@pytest.mark.parametrize("level", BACKEND_EFFORT_LEVELS["deepseek_harness"])
def test_deepseek_passes_its_levels_verbatim(tmp_path: Path, level: str) -> None:
    adapter = DeepSeekHarnessAdapter(
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
        effort=level,
    )

    assert f"--reasoning-effort {level}" in adapter.command_template
    assert adapter.effort == level


@pytest.mark.parametrize("level", ["minimal", "medium", "xhigh", "ultra"])
def test_deepseek_rejects_levels_it_does_not_document(tmp_path: Path, level: str) -> None:
    with pytest.raises(ValueError, match="effort must be one of"):
        DeepSeekHarnessAdapter(
            workspace_path=str(tmp_path),
            prompt_dir=str(tmp_path / "prompts"),
            effort=level,
        )


def test_deepseek_omits_the_flag_without_effort(tmp_path: Path) -> None:
    adapter = DeepSeekHarnessAdapter(
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
    )

    assert "--reasoning-effort" not in adapter.command_template


def test_deepseek_runner_writes_the_effort_patch(tmp_path: Path) -> None:
    """The profile patch carries the llm-deepseek reasoningEffort override."""

    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")
    binary = tmp_path / "dsh"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    assert deepseek_runner_run(str(binary), prompt, "deepseek-v4-flash", "max") == 0

    patch = prompt.with_name(f"{prompt.name}.dsh-model-patch.yml").read_text(encoding="utf-8")
    assert "- id: agent-default-model" in patch
    assert "- id: llm-deepseek" in patch
    assert 'reasoningEffort: "max"' in patch


def test_deepseek_runner_patch_has_no_effort_entry_by_default(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")
    binary = tmp_path / "dsh"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    assert deepseek_runner_run(str(binary), prompt, "deepseek-v4-flash") == 0

    patch = prompt.with_name(f"{prompt.name}.dsh-model-patch.yml").read_text(encoding="utf-8")
    assert "llm-deepseek" not in patch
    assert "reasoningEffort" not in patch


# --- OpenCode: --variant <preset name> --------------------------------------


def test_build_agent_wires_effort_into_opencode_variant(tmp_path: Path) -> None:
    """The wiring spot where the parameter is renamed (`effort`) must not drift."""

    adapter = _build_agent(
        "opencode",
        role="cli_executor",
        model=None,
        api_key=None,
        base_url=None,
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
        effort="medium",
    )

    assert isinstance(adapter, OpenCodeAdapter)
    assert "--variant medium" in adapter.command_template
    assert adapter.effort == "medium"


def test_opencode_passes_custom_variant_names_verbatim(tmp_path: Path) -> None:
    adapter = OpenCodeAdapter(
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
        effort="my-custom-variant",
        role="cli_executor",
    )

    assert "--variant my-custom-variant" in adapter.command_template


# --- episode metadata -------------------------------------------------------


def test_requested_effort_lands_in_episode_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events = "\n".join(
        json.dumps(event)
        for event in (
            {"type": "step_start"},
            {"type": "text", "text": "done"},
            {"type": "step_finish"},
        )
    )
    binary = tmp_path / "bin" / "opencode"
    binary.parent.mkdir(parents=True)
    binary.write_text(f"#!/bin/sh\ncat >/dev/null\ncat <<'EOF'\n{events}\nEOF\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(
        opencode_adapter_module, "resolve_opencode_binary", lambda: str(binary)
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    adapter = OpenCodeAdapter(
        workspace_path=str(workspace),
        prompt_dir=str(tmp_path / "prompts"),
        effort="medium",
        role="cli_executor",
    )
    result = asyncio.run(
        adapter.run_episode(
            "complete the task",
            LocalEnvironment(tmp_dir=str(tmp_path / "tmp")),
            EpisodeBudget(max_duration_seconds=10),
        )
    )

    assert result.metadata["effort"] == "medium"
    assert result.metadata["opencode_variant"] == "medium"


# --- web supervisor boundary ------------------------------------------------


def test_supervisor_accepts_and_forwards_role_effort(tmp_path: Path) -> None:
    from lh_harness.supervisor.service import RunSupervisor, _normalise_role_configs

    resolved = _normalise_role_configs(
        {"auditor": {"agent": "codex", "model": "gpt-test", "effort": "low"}},
        agent="codex",
        model="gpt-test",
    )
    assert resolved["auditor"]["effort"] == "low"
    assert "effort" not in resolved["manager"]

    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "ws")
    command = supervisor._worker_command(
        run_id="r1",
        task="t",
        agent="codex",
        model=None,
        role_configs=resolved,
        workspace=str(tmp_path / "ws"),
        max_rounds=5,
        prompt_language="en",
    )
    assert "--auditor-effort=low" in command
    assert not any("--manager-effort" in item for item in command)


def test_supervisor_validates_effort_against_the_role_backend() -> None:
    from lh_harness.supervisor.service import _normalise_role_configs

    # "max" is a Claude level, not a Codex one - no silent substitution.
    with pytest.raises(ValueError, match="effort must be one of"):
        _normalise_role_configs(
            {"executor": {"agent": "codex", "effort": "max"}}, agent="codex", model=None
        )
    # ... but it is valid verbatim on a Claude role.
    resolved = _normalise_role_configs(
        {"executor": {"agent": "claude_code", "effort": "max"}}, agent="codex", model=None
    )
    assert resolved["executor"]["effort"] == "max"
    # OpenCode variants are per-model names; only their shape is checked.
    resolved = _normalise_role_configs(
        {"executor": {"agent": "opencode", "effort": "my-variant"}}, agent="codex", model=None
    )
    assert resolved["executor"]["effort"] == "my-variant"


def test_snapshot_provenance_keeps_role_effort() -> None:
    from lh_harness.webapi.snapshot import _safe_role_configs

    cleaned = _safe_role_configs(
        {
            "manager": {"agent": "codex", "model": "m", "effort": "high"},
            "executor": {"agent": "opencode", "model": "m", "effort": "my-variant"},
            "auditor": {"agent": "codex", "model": "m", "effort": "x" * 65},
        }
    )

    assert cleaned["manager"]["effort"] == "high"
    assert cleaned["executor"]["effort"] == "my-variant"
    assert "effort" not in cleaned["auditor"]
