from __future__ import annotations

from pathlib import Path

import pytest

from lh_harness import agent_registry
from lh_harness.agent_registry import (
    AGENT_IDS,
    AGENT_SPECS,
    agent_spec,
    normalise_reasoning_effort,
    probe_agents,
    reasoning_choices,
    supports_reasoning_effort,
)
from lh_harness.cli import _AGENT_CHOICES

from .fake_cli import fake_cli


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """The probe cache is module-global; a stale entry would leak between tests."""
    agent_registry._probed = None
    agent_registry._probed_key = None
    agent_registry._probed_at = 0.0
    yield
    agent_registry._probed = None
    agent_registry._probed_key = None
    agent_registry._probed_at = 0.0


def _stub(path: Path, body: str) -> str:
    # A Python-bodied stand-in runs identically on POSIX and on Windows,
    # where `#!/bin/sh` scripts cannot be executed at all.
    return fake_cli(path, body)


def test_cli_agent_choices_match_the_registry() -> None:
    # `cli._AGENTS` stays literal so `--help` needs no registry import, which
    # only works if the two cannot drift.
    assert _AGENT_CHOICES == AGENT_IDS


def test_registry_default_models_match_the_cli_table() -> None:
    from lh_harness.cli import _AGENTS

    assert {name: model for name, _, model in _AGENTS} == {
        spec.id: spec.default_model for spec in AGENT_SPECS
    }
    assert {name: binary for name, binary, _ in _AGENTS} == {
        spec.id: spec.binary for spec in AGENT_SPECS
    }


def test_binary_on_path_but_not_runnable_is_not_reported_usable(tmp_path: Path) -> None:
    # Worse than missing: it looks healthy while breaking every run, so it must
    # never collapse into `usable` or `missing`.
    broken = _stub(tmp_path / "codex", "sys.exit(3)\n")

    probes = probe_agents(binaries={"codex": broken})

    assert probes["codex"].availability == "found_but_broken"
    assert probes["codex"].usable is False
    assert probes["codex"].binary == broken
    assert "not usable" in probes["codex"].problem


def test_binary_that_prints_no_version_is_not_reported_usable(tmp_path: Path) -> None:
    silent = _stub(tmp_path / "codex", "pass\n")

    probes = probe_agents(binaries={"codex": silent})

    assert probes["codex"].availability == "found_but_broken"


def test_missing_binary_is_missing_not_broken() -> None:
    probes = probe_agents(binaries={"codex": ""})

    assert probes["codex"].availability == "missing"
    assert probes["codex"].binary == ""


def test_runnable_binary_reports_its_version(tmp_path: Path) -> None:
    good = _stub(tmp_path / "codex", 'print("codex-cli 9.9.9")\n')

    probes = probe_agents(binaries={"codex": good})

    assert probes["codex"].availability == "usable"
    assert probes["codex"].version == "9.9.9"
    assert probes["codex"].problem == ""


def test_explicit_binary_is_probed_instead_of_rediscovering(tmp_path: Path) -> None:
    # The Web API resolves a path once per request so its response is
    # self-consistent; re-resolving here could describe another installation.
    chosen = _stub(tmp_path / "chosen" / "codex", 'print("1.0.0")\n')

    probes = probe_agents(binaries={"codex": chosen})

    assert probes["codex"].binary == chosen


def test_claude_effort_tiers_are_read_from_cli_help(tmp_path: Path) -> None:
    # `claude --help` wraps the tier list onto a continuation line.
    claude = _stub(
        tmp_path / "claude",
        'if sys.argv[1:] == ["--version"]:\n'
        '    print("2.1.212")\n'
        "    sys.exit(0)\n"
        'print("  --effort <level>                      Effort level for the current session")\n'
        'print("                                        (low, medium, high, xhigh, max)")\n'
        'print("  --other-flag                          Something else")\n',
    )

    probes = probe_agents(binaries={"claude_code": claude})
    spec = agent_spec("claude_code")

    assert probes["claude_code"].discovered_efforts == ("low", "medium", "high", "xhigh", "max")
    assert reasoning_choices(spec, probes["claude_code"]) == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )


def test_effort_discovery_falls_back_to_declared_tiers_when_help_is_unparsable(
    tmp_path: Path,
) -> None:
    claude = _stub(
        tmp_path / "claude",
        'if sys.argv[1:] == ["--version"]:\n'
        '    print("2.1.212")\n'
        "    sys.exit(0)\n"
        'print("  --effort <level>   Effort level (default)")\n',
    )

    probes = probe_agents(binaries={"claude_code": claude})
    spec = agent_spec("claude_code")

    # A single parenthesised word is prose, not a tier list.
    assert probes["claude_code"].discovered_efforts == ()
    assert reasoning_choices(spec, probes["claude_code"]) == spec.reasoning.declared_choices


def test_agents_without_an_effort_switch_declare_no_reasoning() -> None:
    assert supports_reasoning_effort("deepseek_harness") is False
    assert agent_spec("deepseek_harness").reasoning is None
    assert reasoning_choices(agent_spec("deepseek_harness"), None) == ()


def test_every_other_agent_declares_how_it_receives_an_effort() -> None:
    for agent_id in ("codex", "claude_code", "opencode"):
        reasoning = agent_spec(agent_id).reasoning
        assert reasoning is not None
        assert reasoning.declared_choices, agent_id
        assert reasoning.flag


@pytest.mark.parametrize(
    "value",
    [
        'a"b',  # would terminate Codex's inline TOML string
        "a'b",
        "high medium",
        "high\nlow",
        "x" * 65,
        "high;rm -rf /",
        "$(whoami)",
        "`id`",
    ],
)
def test_reasoning_effort_rejects_values_that_could_break_out(value: str) -> None:
    with pytest.raises(ValueError):
        normalise_reasoning_effort(value)


@pytest.mark.parametrize("value", ["high", "ultra", "x-high", "a.b_c:d", "MAX", "2"])
def test_reasoning_effort_accepts_values_beyond_the_known_tiers(value: str) -> None:
    # Codex accepts `ultra` client-side even though its API enumerates a shorter
    # set, so an allow-list would reject values that actually work.
    assert normalise_reasoning_effort(value) == value


def test_blank_reasoning_effort_means_follow_the_provider_default() -> None:
    assert normalise_reasoning_effort(None) == ""
    assert normalise_reasoning_effort("") == ""
    assert normalise_reasoning_effort("   ") == ""


def test_reasoning_effort_is_rejected_for_an_agent_without_the_switch() -> None:
    with pytest.raises(ValueError, match="does not accept a reasoning effort"):
        normalise_reasoning_effort("high", agent_id="deepseek_harness")
    # Blank stays acceptable: it asks for nothing.
    assert normalise_reasoning_effort("", agent_id="deepseek_harness") == ""


def test_unknown_agent_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown agent"):
        agent_spec("not-an-agent")
