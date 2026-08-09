"""Tier selection in the run loop, and its independence from executor type."""

from __future__ import annotations

import pytest
from conftest import audit_report, manager_plan
from loop_harness import executor_calls, run_loop

from lh_harness.types import ExecutorRouting

# Escalation off, so these tests isolate manager choice and the default.
NO_ESCALATION = ExecutorRouting(escalate_after_failures=0)


def one_round(config_factory, plan, **routing):
    config = config_factory(
        max_total_episodes=1,
        executor_routing=ExecutorRouting(escalate_after_failures=0, **routing),
    )
    return run_loop(config, plans=[plan], audits=[audit_report(passing=True)])


def test_manager_selected_cheap_uses_the_cheap_executor(harness_config):
    run = one_round(harness_config, manager_plan("cli", tier="cheap"))
    assert executor_calls(run) == {"cli/cheap": 1}


def test_manager_selected_strong_uses_the_strong_executor(harness_config):
    run = one_round(harness_config, manager_plan("cli", tier="strong"))
    assert executor_calls(run) == {"cli/strong": 1}


def test_no_tier_falls_back_to_the_default(harness_config):
    run = one_round(harness_config, manager_plan("cli"))
    assert executor_calls(run) == {"cli/cheap": 1}


def test_default_tier_is_configurable(harness_config):
    run = one_round(harness_config, manager_plan("cli"), default_tier="strong")
    assert executor_calls(run) == {"cli/strong": 1}


def test_unrecognized_tier_falls_back_without_failing_the_round(harness_config):
    run = one_round(harness_config, manager_plan("cli", tier="medium"))
    assert executor_calls(run) == {"cli/cheap": 1}
    assert run.report["rounds"][0]["executor_tier"] == "cheap"


# --- executor type x tier ---------------------------------------------------


@pytest.mark.parametrize("executor_type", ["gui", "cli"])
@pytest.mark.parametrize("tier", ["cheap", "strong"])
def test_every_type_and_tier_combination_routes_to_its_own_cell(
    harness_config, executor_type, tier
):
    run = one_round(harness_config, manager_plan(executor_type, tier=tier))
    assert executor_calls(run) == {f"{executor_type}/{tier}": 1}


@pytest.mark.parametrize("executor_type", ["gui", "cli"])
@pytest.mark.parametrize("tier", ["cheap", "strong"])
def test_tier_does_not_change_which_auditor_runs(harness_config, executor_type, tier):
    """The auditor is still chosen by executor type alone."""
    run = one_round(harness_config, manager_plan(executor_type, tier=tier))
    other = "cli" if executor_type == "gui" else "gui"
    assert run.auditors[executor_type].calls == 1
    assert run.auditors[other].calls == 0


@pytest.mark.parametrize(
    ("executor_type", "expected_seconds"),
    [("gui", 7), ("cli", 9)],
)
@pytest.mark.parametrize("tier", ["cheap", "strong"])
def test_tier_does_not_change_the_episode_budget(
    harness_config, executor_type, expected_seconds, tier
):
    """Budgets stay per executor type: a tier changes the model, not the clock."""
    run = one_round(harness_config, manager_plan(executor_type, tier=tier))
    episode = run.executor(executor_type, tier).episodes[0]
    assert episode.budget_seconds == expected_seconds


# --- recorded state ---------------------------------------------------------


def test_tier_is_recorded_on_the_round_and_in_the_events(harness_config):
    run = one_round(harness_config, manager_plan("cli", tier="strong"))
    assert run.report["rounds"][0]["executor_tier"] == "strong"
    assert run.rounds_jsonl()[0]["executor_tier"] == "strong"
    start = run.events("executor_role_start")[0]
    assert (start["executor_tier"], start["tier_source"]) == ("strong", "manager")
    assert run.events("executor_role_done")[0]["executor_tier"] == "strong"


def test_tier_source_distinguishes_a_default_from_a_manager_choice(harness_config):
    run = one_round(harness_config, manager_plan("cli"))
    assert run.events("executor_role_start")[0]["tier_source"] == "default"


def test_round_directory_exposes_the_tier_for_the_dashboard(harness_config):
    run = one_round(harness_config, manager_plan("cli", tier="strong"))
    assert (run.round_dir(1) / "executor_tier.txt").read_text(encoding="utf-8") == "strong"


def test_report_carries_the_routing_policy_and_what_it_did(harness_config):
    run = one_round(harness_config, manager_plan("cli", tier="strong"))
    routing = run.report["executor_routing"]
    assert routing["default_tier"] == "cheap"
    assert routing["escalate_after_failures"] == 0
    assert routing["escalated"] is False
    assert routing["rounds_by_tier"] == {"strong": 1}


def test_progress_events_carry_the_tier(harness_config):
    run = one_round(harness_config, manager_plan("cli", tier="strong"))
    done = [
        payload
        for event, payload in run.progress
        if event == "role_done" and payload.get("role") == "cli_executor"
    ]
    assert done[0]["executor_tier"] == "strong"


def test_rounds_that_never_reach_an_executor_have_no_tier(harness_config):
    config = harness_config(max_total_episodes=1, executor_routing=NO_ESCALATION)
    run = run_loop(config, plans=[manager_plan("blocked")], audits=[])
    assert run.report["rounds"][0]["executor_tier"] == ""
    assert executor_calls(run) == {}
