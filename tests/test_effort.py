"""Per-role effort: config keys, CLI fallback chain, adapter dials.

The harness exposes one normalized scale — min/low/med/high/xhigh/max,
covering the union of the levels the backends document — and each adapter
translates a level into its own documented dial and vocabulary, mapping a
level its backend lacks to the nearest supported one.  Episode metadata
records both the requested and the effective value so substitutions stay
visible.
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
from lh_harness.types import EpisodeBudget, EFFORT_CHOICES


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


@pytest.mark.parametrize("bad", ["ultra", "", 3, True, ["high"]])
def test_config_rejects_unknown_effort_values(bad: object) -> None:
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
    args = _args(executor_effort="med")

    assert _resolve_role_effort(args, "cli_executor") == "med"
    assert _resolve_role_effort(args, "manager") is None


def test_effort_falls_back_to_the_global_flag() -> None:
    args = _args(effort="min")

    assert _resolve_role_effort(args, "auditor") == "min"


def test_effort_defaults_to_backend_default() -> None:
    assert _resolve_role_effort(_args(), "manager") is None


# --- Codex: -c model_reasoning_effort (minimal|low|medium|high|xhigh) ------


def test_codex_passes_native_levels_through(tmp_path: Path) -> None:
    adapter = CodexAdapter(
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
        effort="xhigh",
    )

    assert 'model_reasoning_effort="xhigh"' in adapter.command_template
    assert adapter.effort == "xhigh"
    assert adapter.effort_effective == "xhigh"


def test_codex_maps_max_to_its_top_level(tmp_path: Path) -> None:
    """Codex documents no "max"; the request lands on xhigh, visibly."""

    adapter = CodexAdapter(
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
        effort="max",
    )

    assert 'model_reasoning_effort="xhigh"' in adapter.command_template
    assert adapter.effort == "max"
    assert adapter.effort_effective == "xhigh"


def test_codex_omits_the_override_without_effort(tmp_path: Path) -> None:
    adapter = CodexAdapter(
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
    )

    assert "model_reasoning_effort" not in adapter.command_template


def test_codex_rejects_unknown_effort(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="effort"):
        CodexAdapter(
            workspace_path=str(tmp_path),
            prompt_dir=str(tmp_path / "prompts"),
            effort="ultra",
        )


# --- Claude Code: CLAUDE_CODE_EFFORT_LEVEL (low|medium|high|xhigh|max) -----


@pytest.mark.parametrize(
    ("effort", "level"),
    [
        ("min", "low"),  # Claude documents no "min"; floor is low
        ("low", "low"),
        ("med", "medium"),
        ("high", "high"),
        ("xhigh", "xhigh"),
        ("max", "max"),
    ],
)
def test_claude_sets_the_effort_level_env(tmp_path: Path, effort: str, level: str) -> None:
    adapter = ClaudeCodeAdapter(
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
        effort=effort,
    )

    assert f"CLAUDE_CODE_EFFORT_LEVEL={level}" in adapter.command_template
    assert adapter.effort == effort
    assert adapter.effort_effective == level


def test_claude_omits_the_env_without_effort(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter(
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
    )

    assert "CLAUDE_CODE_EFFORT_LEVEL" not in adapter.command_template


def test_claude_rejects_unknown_effort(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="effort"):
        ClaudeCodeAdapter(
            workspace_path=str(tmp_path),
            prompt_dir=str(tmp_path / "prompts"),
            effort="ultra",
        )


# --- DeepSeek Harness: reasoningEffort patch (low|high|max) ----------------


@pytest.mark.parametrize(
    ("effort", "dsh_level"),
    [
        ("min", "low"),
        ("low", "low"),
        ("med", "high"),  # DeepSeek's own docs map medium onto high
        ("high", "high"),
        ("xhigh", "high"),  # ... and xhigh onto high
        ("max", "max"),
    ],
)
def test_deepseek_maps_effort_onto_dsh_levels(
    tmp_path: Path, effort: str, dsh_level: str
) -> None:
    adapter = DeepSeekHarnessAdapter(
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
        effort=effort,
    )

    assert f"--reasoning-effort {dsh_level}" in adapter.command_template
    assert adapter.effort == effort
    assert adapter.effort_effective == dsh_level


def test_deepseek_omits_the_flag_without_effort(tmp_path: Path) -> None:
    adapter = DeepSeekHarnessAdapter(
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
    )

    assert "--reasoning-effort" not in adapter.command_template


def test_deepseek_rejects_unknown_effort(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="effort"):
        DeepSeekHarnessAdapter(
            workspace_path=str(tmp_path),
            prompt_dir=str(tmp_path / "prompts"),
            effort="ultra",
        )


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


# --- OpenCode: --variant <preset name> -------------------------------------


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
        effort="med",
    )

    assert isinstance(adapter, OpenCodeAdapter)
    assert "--variant medium" in adapter.command_template
    assert adapter.effort == "med"
    assert adapter.effort_effective == "medium"


def test_opencode_spells_min_out_to_the_minimal_variant(tmp_path: Path) -> None:
    """OpenAI-style models ship a "minimal" variant; the scale's "min" maps to it."""

    adapter = OpenCodeAdapter(
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
        effort="min",
        role="cli_executor",
    )

    assert "--variant minimal" in adapter.command_template
    assert adapter.effort == "min"
    assert adapter.effort_effective == "minimal"


# --- episode metadata ------------------------------------------------------


def test_requested_and_effective_effort_land_in_episode_metadata(
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
        effort="med",
        role="cli_executor",
    )
    result = asyncio.run(
        adapter.run_episode(
            "complete the task",
            LocalEnvironment(tmp_dir=str(tmp_path / "tmp")),
            EpisodeBudget(max_duration_seconds=10),
        )
    )

    assert result.metadata["effort"] == "med"
    assert result.metadata["effort_effective"] == "medium"
    assert result.metadata["opencode_variant"] == "medium"


def test_every_choice_has_a_translation_in_every_backend() -> None:
    from lh_harness.adapters.claude_code import _CLAUDE_EFFORT_LEVELS
    from lh_harness.adapters.deepseek_harness import _DSH_EFFORTS

    assert set(_CLAUDE_EFFORT_LEVELS) == set(EFFORT_CHOICES)
    assert set(_DSH_EFFORTS) == set(EFFORT_CHOICES)


# --- web supervisor boundary -----------------------------------------------


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


def test_supervisor_rejects_bad_role_effort() -> None:
    from lh_harness.supervisor.service import _normalise_role_configs

    with pytest.raises(ValueError, match="effort must be one of"):
        _normalise_role_configs(
            {"executor": {"effort": "ultra"}}, agent="codex", model=None
        )


def test_snapshot_provenance_keeps_valid_role_effort() -> None:
    from lh_harness.webapi.snapshot import _safe_role_configs

    cleaned = _safe_role_configs(
        {
            "manager": {"agent": "codex", "model": "m", "effort": "high"},
            "executor": {"agent": "codex", "model": "m", "effort": "bogus"},
            "auditor": {"agent": "codex", "model": "m"},
        }
    )

    assert cleaned["manager"]["effort"] == "high"
    assert "effort" not in cleaned["executor"]
    assert "effort" not in cleaned["auditor"]
