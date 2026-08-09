"""Project-configuration support for executor tiers and routing."""

from __future__ import annotations

import pytest

from lh_harness.config import CONFIG_TEMPLATE, ProjectConfigError, load_run_defaults
from lh_harness.types import (
    DEFAULT_ESCALATE_AFTER_FAILURES,
    DEFAULT_ESCALATION_TIER,
    DEFAULT_EXECUTOR_TIER,
)

# What the pre-tier config system produced for a plain single-executor project.
# Locked as a literal so a regression here is unmistakable.
LEGACY_CONFIG = """\
[run]
agent = "codex"

[run.roles.executor]
agent = "claude_code"
model = "claude-opus-5"
"""
LEGACY_DEFAULTS = {
    "agent": "codex",
    "executor_agent": "claude_code",
    "executor_model": "claude-opus-5",
}


def load(tmp_path, text, name="config.toml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return load_run_defaults(path)


def test_cheap_and_strong_executors_take_different_backends(tmp_path):
    defaults = load(
        tmp_path,
        """
[run.roles.executor.cheap]
agent = "codex"
model = "cheap-model"

[run.roles.executor.strong]
agent = "claude_code"
model = "strong-model"
""",
    )
    assert defaults == {
        "executor_cheap_agent": "codex",
        "executor_cheap_model": "cheap-model",
        "executor_strong_agent": "claude_code",
        "executor_strong_model": "strong-model",
    }


@pytest.mark.parametrize("role", ["gui_executor", "cli_executor"])
def test_executor_type_can_override_a_single_tier(tmp_path, role):
    defaults = load(tmp_path, f'[run.roles.{role}.strong]\nmodel = "type-strong"\n')
    assert defaults == {f"{role}_strong_model": "type-strong"}


def test_partial_tier_table_only_sets_what_it_names(tmp_path):
    defaults = load(tmp_path, '[run.roles.executor.cheap]\nmodel = "only-a-model"\n')
    assert defaults == {"executor_cheap_model": "only-a-model"}


def test_role_table_may_mix_plain_keys_and_tier_tables(tmp_path):
    defaults = load(
        tmp_path,
        """
[run.roles.executor]
agent = "codex"

[run.roles.executor.strong]
agent = "claude_code"
""",
    )
    assert defaults == {"executor_agent": "codex", "executor_strong_agent": "claude_code"}


def test_executor_routing_table(tmp_path):
    defaults = load(
        tmp_path,
        """
[run.executor_routing]
default_tier = "cheap"
escalate_after_failures = 3
escalation_tier = "strong"
escalation_briefing = false
""",
    )
    assert defaults == {
        "executor_default_tier": "cheap",
        "executor_escalate_after_failures": 3,
        "executor_escalation_tier": "strong",
        "executor_escalation_briefing": False,
    }


def test_escalation_can_be_disabled_with_zero(tmp_path):
    defaults = load(tmp_path, "[run.executor_routing]\nescalate_after_failures = 0\n")
    assert defaults["executor_escalate_after_failures"] == 0


# --- backward compatibility -------------------------------------------------


def test_existing_single_executor_config_is_unchanged(tmp_path):
    assert load(tmp_path, LEGACY_CONFIG) == LEGACY_DEFAULTS


def test_config_without_tiers_gains_no_routing_keys(tmp_path):
    defaults = load(tmp_path, LEGACY_CONFIG)
    assert not [key for key in defaults if key.startswith("executor_default")]
    assert not [key for key in defaults if key.startswith("executor_escalat")]


def test_generated_template_still_loads(tmp_path):
    defaults = load(tmp_path, CONFIG_TEMPLATE)
    # The template's commented-out tier tables must not invent any role values.
    assert not [key for key in defaults if "_cheap_" in key or "_strong_" in key]
    # It does document the routing defaults, which must equal the built-in ones.
    assert defaults["executor_default_tier"] == DEFAULT_EXECUTOR_TIER
    assert defaults["executor_escalation_tier"] == DEFAULT_ESCALATION_TIER
    assert defaults["executor_escalate_after_failures"] == DEFAULT_ESCALATE_AFTER_FAILURES


# --- invalid configuration --------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('[run.roles.executor.medium]\nagent = "codex"\n', "tier(s): medium"),
        ('[run.roles.manager.cheap]\nagent = "codex"\n', "does not take executor tiers"),
        ('[run.roles.auditor.strong]\nagent = "codex"\n', "does not take executor tiers"),
        ('[run.roles.executor.cheap]\nnope = 1\n', "[run.roles.executor.cheap] key(s): nope"),
        ('[run.executor_routing]\ndefault_tier = "medium"\n', "must be one of: cheap, strong"),
        ('[run.executor_routing]\nescalation_tier = "fast"\n', "must be one of: cheap, strong"),
        ("[run.executor_routing]\nescalation_tier = 7\n", "must be a non-empty string"),
        ("[run.executor_routing]\nescalate_after_failures = -1\n", "0 or more"),
        ('[run.executor_routing]\nescalate_after_failures = "two"\n', "0 or more"),
        ('[run.executor_routing]\nescalation_briefing = "yes"\n', "must be true or false"),
        ("[run.executor_routing]\nnope = 1\n", "[run.executor_routing] key(s): nope"),
        ("[run]\nexecutor_routing = 5\n", "must be a TOML table"),
    ],
)
def test_invalid_tier_configuration_is_rejected(tmp_path, text, expected):
    with pytest.raises(ProjectConfigError) as excinfo:
        load(tmp_path, text)
    assert expected in str(excinfo.value)


def test_unknown_tier_error_names_the_valid_tiers(tmp_path):
    with pytest.raises(ProjectConfigError) as excinfo:
        load(tmp_path, '[run.roles.executor.medium]\nagent = "codex"\n')
    assert "expected: cheap, strong" in str(excinfo.value)


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("executor_cheap", "[run.roles.executor.cheap]"),
        ("gui_executor_strong", "[run.roles.gui_executor.strong]"),
        ("cli_executor_cheap", "[run.roles.cli_executor.cheap]"),
    ],
)
def test_flat_tier_role_name_points_at_the_nested_table(tmp_path, role, expected):
    """The CLI flag is --executor-cheap-agent, so the flat name is an easy guess."""
    with pytest.raises(ProjectConfigError) as excinfo:
        load(tmp_path, f'[run.roles.{role}]\nagent = "codex"\n')
    assert expected in str(excinfo.value)


def test_an_ordinary_unknown_role_still_gets_the_plain_error(tmp_path):
    with pytest.raises(ProjectConfigError) as excinfo:
        load(tmp_path, '[run.roles.reviewer]\nagent = "codex"\n')
    assert str(excinfo.value) == "unknown role(s): reviewer"
