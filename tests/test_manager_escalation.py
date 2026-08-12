"""Cost-aware escalation: when the harness overrides the tier, and when it does not."""

from __future__ import annotations

import pytest
from tests.conftest import audit_report, manager_plan
from tests.loop_harness import executor_calls, run_loop

from lh_harness.manager import _ExecutorRouter
from lh_harness.types import AuditReport, ExecutorRouting

FAIL = audit_report(passing=False)
PASS = audit_report(passing=True)


def run_rounds(config_factory, verdicts, *, tier=None, route="cli", **routing):
    """Run one round per verdict, all on the same route."""
    config = config_factory(
        max_total_episodes=len(verdicts),
        executor_routing=ExecutorRouting(**routing),
    )
    return run_loop(
        config,
        plans=[manager_plan(route, tier=tier) for _ in verdicts],
        audits=list(verdicts),
    )


# --- the router in isolation ------------------------------------------------


def report(*, passing):
    return AuditReport(
        round_id="round_1",
        status="complete" if passing else "incomplete",
        integrity_status="clean" if passing else "suspect",
        contract_audit_status="aligned" if passing else "unknown",
    )


def test_router_escalates_only_at_the_threshold():
    router = _ExecutorRouter(routing=ExecutorRouting(escalate_after_failures=2))
    assert router.record_audit(report(passing=False), 1, "cheap") is None
    assert router.select(None) == ("cheap", "default")

    escalation = router.record_audit(report(passing=False), 2, "cheap")
    assert escalation is not None
    assert escalation["round"] == 2
    assert escalation["from_tier"] == "cheap"
    assert escalation["to_tier"] == "strong"
    assert escalation["failed_rounds"] == [1, 2]
    assert router.select(None) == ("strong", "escalated")


def test_router_escalation_outranks_a_manager_hint():
    router = _ExecutorRouter(routing=ExecutorRouting(escalate_after_failures=1))
    router.record_audit(report(passing=False), 1, "cheap")
    assert router.select("cheap") == ("strong", "escalated")


def test_router_resets_on_a_passing_audit():
    router = _ExecutorRouter(routing=ExecutorRouting(escalate_after_failures=1))
    router.record_audit(report(passing=False), 1, "cheap")
    assert router.escalated is True

    assert router.record_audit(report(passing=True), 2, "strong") is None
    assert router.escalated is False
    assert router.consecutive_audit_failures == 0
    assert router.failed_round_indices == []
    assert router.select(None) == ("cheap", "default")


def test_router_failure_run_is_consecutive_not_cumulative():
    router = _ExecutorRouter(routing=ExecutorRouting(escalate_after_failures=2))
    router.record_audit(report(passing=False), 1, "cheap")
    router.record_audit(report(passing=True), 2, "cheap")
    assert router.record_audit(report(passing=False), 3, "cheap") is None
    assert router.escalated is False


def test_router_never_escalates_when_disabled():
    router = _ExecutorRouter(routing=ExecutorRouting(escalate_after_failures=0))
    for index in range(1, 6):
        assert router.record_audit(report(passing=False), index, "cheap") is None
    assert router.escalated is False


def test_router_has_nowhere_to_escalate_from_the_escalation_tier():
    """Failing work already on the strong tier makes no escalation claim."""
    router = _ExecutorRouter(routing=ExecutorRouting(escalate_after_failures=1))
    assert router.record_audit(report(passing=False), 1, "strong") is None
    assert router.escalated is False
    assert router.consecutive_audit_failures == 1


# --- through the loop -------------------------------------------------------


def test_the_default_escalates_on_the_first_failed_audit(harness_config):
    """A same-tier retry is not briefed, so the default does not spend a round on one."""
    config = harness_config(max_total_episodes=2, executor_routing=ExecutorRouting())
    run = run_loop(config, plans=[manager_plan("cli"), manager_plan("cli")], audits=[FAIL, PASS])
    assert ExecutorRouting().escalate_after_failures == 1
    assert run.tiers == ["cheap", "strong"]


def test_raising_the_threshold_retries_on_the_cheap_tier_first(harness_config):
    run = run_rounds(harness_config, [FAIL, FAIL, PASS], escalate_after_failures=2)
    assert run.tiers == ["cheap", "cheap", "strong"]


def test_one_failure_does_not_escalate(harness_config):
    run = run_rounds(harness_config, [FAIL, FAIL], escalate_after_failures=3)
    assert run.tiers == ["cheap", "cheap"]
    assert run.events("executor_escalation") == []


def test_reaching_the_threshold_switches_to_the_strong_executor(harness_config):
    run = run_rounds(harness_config, [FAIL, FAIL, PASS], escalate_after_failures=2)
    assert run.tiers == ["cheap", "cheap", "strong"]
    assert executor_calls(run) == {"cli/cheap": 2, "cli/strong": 1}


def test_escalation_emits_one_event_with_its_reason(harness_config):
    run = run_rounds(harness_config, [FAIL, FAIL, PASS], escalate_after_failures=2)
    events = run.events("executor_escalation")
    assert len(events) == 1
    assert events[0]["round"] == 2
    assert events[0]["from_tier"] == "cheap"
    assert events[0]["to_tier"] == "strong"
    assert events[0]["reason"] == "audit_failure_threshold_reached"
    assert events[0]["consecutive_audit_failures"] == 2
    assert events[0]["threshold"] == 2
    assert events[0]["failed_rounds"] == [1, 2]


def test_escalation_reaches_the_progress_sink(harness_config):
    run = run_rounds(harness_config, [FAIL, FAIL, PASS], escalate_after_failures=2)
    escalations = [payload for event, payload in run.progress if event == "executor_escalation"]
    assert len(escalations) == 1
    assert (escalations[0]["from_tier"], escalations[0]["to_tier"]) == ("cheap", "strong")


def test_once_escalated_later_failing_rounds_stay_strong(harness_config):
    run = run_rounds(harness_config, [FAIL, FAIL, FAIL, FAIL], escalate_after_failures=2)
    assert run.tiers == ["cheap", "cheap", "strong", "strong"]
    # Escalation is announced once, not on every subsequent round.
    assert len(run.events("executor_escalation")) == 1


def test_escalation_does_not_bypass_the_auditor(harness_config):
    run = run_rounds(harness_config, [FAIL, FAIL, PASS], escalate_after_failures=2)
    assert run.auditors["cli"].calls == 3
    assert (run.round_dir(3) / "auditor_report.txt").is_file()
    assert run.report["rounds"][2]["auditor_report"].startswith("Status: complete")


def test_a_pass_returns_routing_to_the_default_tier(harness_config):
    run = run_rounds(harness_config, [FAIL, FAIL, PASS, FAIL], escalate_after_failures=2)
    assert run.tiers == ["cheap", "cheap", "strong", "cheap"]


def test_successful_cheap_work_never_invokes_the_strong_executor(harness_config):
    run = run_rounds(harness_config, [PASS, PASS, PASS], escalate_after_failures=2)
    assert run.tiers == ["cheap", "cheap", "cheap"]
    assert executor_calls(run) == {"cli/cheap": 3}
    assert run.report["executor_routing"]["escalated"] is False


def test_escalation_can_be_disabled(harness_config):
    run = run_rounds(harness_config, [FAIL, FAIL, FAIL, FAIL], escalate_after_failures=0)
    assert run.tiers == ["cheap"] * 4
    assert run.events("executor_escalation") == []


def test_a_manager_strong_hint_is_not_an_escalation(harness_config):
    run = run_rounds(harness_config, [FAIL, FAIL], tier="strong", escalate_after_failures=2)
    assert run.tiers == ["strong", "strong"]
    assert run.events("executor_escalation") == []
    assert run.report["executor_routing"]["escalated"] is False


@pytest.mark.parametrize("route", ["gui", "cli"])
def test_escalation_works_for_both_executor_types(harness_config, route):
    run = run_rounds(harness_config, [FAIL, FAIL, PASS], route=route, escalate_after_failures=2)
    assert run.tiers == ["cheap", "cheap", "strong"]
    assert executor_calls(run) == {f"{route}/cheap": 2, f"{route}/strong": 1}


def test_report_records_the_escalation_and_per_tier_counts(harness_config):
    run = run_rounds(harness_config, [FAIL, FAIL, FAIL], escalate_after_failures=2)
    routing = run.report["executor_routing"]
    assert routing["escalated"] is True
    assert routing["escalated_from"] == "cheap"
    assert routing["rounds_by_tier"] == {"cheap": 2, "strong": 1}


def test_a_recovered_run_still_reports_that_it_escalated(harness_config):
    """`escalated` is live state; the history must outlive the recovery."""
    run = run_rounds(harness_config, [FAIL, FAIL, PASS, PASS], escalate_after_failures=2)
    routing = run.report["executor_routing"]
    assert routing["escalated"] is False
    assert [item["round"] for item in routing["escalations"]] == [2]


def test_a_run_that_never_escalated_has_no_escalation_history(harness_config):
    run = run_rounds(harness_config, [PASS, PASS], escalate_after_failures=2)
    assert run.report["executor_routing"]["escalations"] == []


def test_escalating_twice_in_one_run_is_recorded_twice(harness_config):
    run = run_rounds(
        harness_config, [FAIL, FAIL, PASS, FAIL, FAIL, PASS], escalate_after_failures=2
    )
    assert run.tiers == ["cheap", "cheap", "strong", "cheap", "cheap", "strong"]
    assert [item["round"] for item in run.report["executor_routing"]["escalations"]] == [2, 5]
