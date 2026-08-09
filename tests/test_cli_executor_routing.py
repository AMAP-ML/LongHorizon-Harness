"""How a (type, tier) cell resolves to a concrete agent and model."""

from __future__ import annotations

import argparse

import pytest

from lh_harness.cli import (
    _EXECUTOR_TIER_ROLES,
    _ROLE_OPTIONS,
    _fallback_chain,
    _resolve_role_option,
)


def ns(**overrides):
    """An argparse namespace shaped like a parsed `run` command."""
    values = {f"{role}_{suffix}": None for role, _, _ in _ROLE_OPTIONS for suffix in ("agent", "model")}
    values.update(agent="codex", model=None)
    values.update(overrides)
    return argparse.Namespace(**values)


def cells(args):
    return {
        f"{executor_type}/{tier}": (
            _resolve_role_option(args, role, "agent"),
            _resolve_role_option(args, role, "model"),
        )
        for (executor_type, tier), role in _EXECUTOR_TIER_ROLES.items()
    }


# --- the fallback chain itself ---------------------------------------------


def test_tier_outranks_type_in_the_chain():
    assert _fallback_chain("gui_executor_cheap") == [
        "gui_executor_cheap",
        "executor_cheap",
        "gui_executor",
        "executor",
    ]


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("manager", ["manager"]),
        ("executor", ["executor"]),
        ("gui_executor", ["gui_executor", "executor"]),
        ("cli_executor", ["cli_executor", "executor"]),
        ("auditor", ["auditor"]),
        ("gui_auditor", ["gui_auditor", "auditor"]),
        ("cli_auditor", ["cli_auditor", "auditor"]),
        ("final_response", ["final_response", "manager"]),
    ],
)
def test_pre_existing_roles_keep_their_chain(role, expected):
    assert _fallback_chain(role) == expected


def test_every_role_chain_terminates():
    for role, _, _ in _ROLE_OPTIONS:
        chain = _fallback_chain(role)
        assert chain[0] == role
        assert len(chain) == len(set(chain))


# --- resolution -------------------------------------------------------------


def test_global_agent_backs_every_cell():
    assert cells(ns()) == {key: ("codex", None) for key in cells(ns())}


def test_single_executor_config_backs_every_cell():
    """Back-compat: a project that configured one executor gets it everywhere."""
    resolved = cells(ns(executor_agent="claude_code", executor_model="opus"))
    assert set(resolved.values()) == {("claude_code", "opus")}


def test_tier_config_splits_both_executor_types():
    resolved = cells(
        ns(
            executor_cheap_agent="codex",
            executor_cheap_model="cheap-m",
            executor_strong_agent="claude_code",
            executor_strong_model="strong-m",
        )
    )
    assert resolved == {
        "gui/cheap": ("codex", "cheap-m"),
        "cli/cheap": ("codex", "cheap-m"),
        "gui/strong": ("claude_code", "strong-m"),
        "cli/strong": ("claude_code", "strong-m"),
    }


def test_tier_beats_type_where_both_are_configured():
    resolved = cells(
        ns(
            gui_executor_agent="claude_code",
            gui_executor_model="gui-m",
            executor_cheap_agent="codex",
            executor_cheap_model="cheap-m",
        )
    )
    # The cheap tier was named explicitly, so it wins for GUI+cheap...
    assert resolved["gui/cheap"] == ("codex", "cheap-m")
    # ...but no strong tier was configured, so the type-level setting still applies.
    assert resolved["gui/strong"] == ("claude_code", "gui-m")


def test_type_and_tier_override_is_the_most_specific():
    resolved = cells(
        ns(
            executor_strong_agent="claude_code",
            executor_strong_model="strong-m",
            gui_executor_strong_model="gui-strong-m",
        )
    )
    assert resolved["gui/strong"] == ("claude_code", "gui-strong-m")
    assert resolved["cli/strong"] == ("claude_code", "strong-m")


def test_agent_and_model_resolve_independently():
    """A tier may set only a model without dragging in another backend's agent."""
    resolved = cells(ns(agent="claude_code", executor_cheap_model="cheap-m"))
    assert resolved["cli/cheap"] == ("claude_code", "cheap-m")


def test_all_four_combinations_can_differ():
    resolved = cells(
        ns(
            gui_executor_cheap_agent="codex",
            gui_executor_cheap_model="a",
            gui_executor_strong_agent="claude_code",
            gui_executor_strong_model="b",
            cli_executor_cheap_agent="codex",
            cli_executor_cheap_model="c",
            cli_executor_strong_agent="claude_code",
            cli_executor_strong_model="d",
        )
    )
    assert sorted(model for _, model in resolved.values()) == ["a", "b", "c", "d"]


def test_pre_existing_role_resolution_is_unchanged():
    args = ns(auditor_agent="claude_code", manager_model="mm")
    assert _resolve_role_option(args, "gui_auditor", "agent") == "claude_code"
    assert _resolve_role_option(args, "cli_auditor", "agent") == "claude_code"
    assert _resolve_role_option(args, "final_response", "model") == "mm"
    assert _resolve_role_option(args, "gui_executor", "agent") == "codex"
