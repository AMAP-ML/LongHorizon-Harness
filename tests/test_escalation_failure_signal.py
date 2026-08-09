"""What counts as an escalation-worthy failure.

Regression cover for a bug found only by a real run: the router originally
treated any audit that was not `complete/clean/aligned` as a failure. Auditors
are instructed to report `incomplete` whenever the whole contract is unfinished
"even if the local subtask succeeded", so a real round-one exploration step came
back `incomplete / clean / aligned` and escalated the run immediately. Both real
end-to-end runs escalated on round 1 that way.
"""

from __future__ import annotations

import pytest
from conftest import audit_report, manager_plan, progress_report
from loop_harness import executor_calls, run_loop

from lh_harness.manager import (
    _audit_gap_fingerprint,
    _audit_reports_a_problem,
    _ExecutorRouter,
    _same_gap,
)
from lh_harness.types import AuditReport, ExecutorRouting

PROBLEM = audit_report(passing=False)
PASS = audit_report(passing=True)


def report(status="incomplete", integrity="clean", contract="aligned", guidance="close the gap"):
    return AuditReport(
        round_id="round_1",
        status=status,
        integrity_status=integrity,
        contract_audit_status=contract,
        action_guidance=guidance,
    )


# --- the predicate ----------------------------------------------------------


def test_clean_but_incomplete_is_progress_not_a_problem():
    """The exact verdict both real E2E rounds produced."""
    assert _audit_reports_a_problem(report()) is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "blocked"},
        {"integrity": "suspect"},
        {"integrity": "violation"},
        {"contract": "needs_revision"},
        {"contract": "invalid"},
    ],
)
def test_real_problems_are_flagged(kwargs):
    assert _audit_reports_a_problem(report(**kwargs)) is True


def test_contract_unknown_alone_is_not_a_problem():
    """`unknown` means undetermined, which is normal early in a run."""
    assert _audit_reports_a_problem(report(contract="unknown")) is False


# --- gap fingerprinting -----------------------------------------------------


def test_gap_prefers_the_auditors_guidance():
    assert _audit_gap_fingerprint(report(guidance="  Write   THE File ")) == "write the file"


def test_gap_falls_back_when_there_is_no_guidance():
    assert _audit_gap_fingerprint(
        AuditReport(round_id="r", status="incomplete", state_summary="Outstanding: X")
    ) == "outstanding: x"


def test_reworded_gaps_still_count_as_the_same():
    assert _same_gap("the report file was never written", "the report file was not written")


def test_different_gaps_are_distinguished():
    assert not _same_gap("write the report file", "install the database driver")


def test_empty_gaps_never_match():
    assert not _same_gap("", "")


# --- the router -------------------------------------------------------------


def test_progress_rounds_never_escalate():
    router = _ExecutorRouter(routing=ExecutorRouting(escalate_after_failures=1))
    for index in range(1, 6):
        escalation = router.record_audit(report(guidance=f"step {index}"), index, "cheap")
        assert escalation is None
    assert router.escalated is False
    assert router.consecutive_audit_failures == 0


def test_a_real_problem_still_escalates_at_the_threshold():
    router = _ExecutorRouter(routing=ExecutorRouting(escalate_after_failures=1))
    escalation = router.record_audit(report(integrity="suspect"), 1, "cheap")
    assert escalation is not None
    assert escalation["reason"] == "audit_failure_threshold_reached"


def test_an_errored_executor_counts_even_when_the_audit_looks_clean():
    router = _ExecutorRouter(routing=ExecutorRouting(escalate_after_failures=1))
    escalation = router.record_audit(report(), 1, "cheap", executor_failed=True)
    assert escalation is not None
    assert escalation["reason"] == "audit_failure_threshold_reached"


def test_the_same_gap_repeated_escalates_as_a_stall():
    router = _ExecutorRouter(
        routing=ExecutorRouting(escalate_after_failures=0, escalate_after_stalled_rounds=3)
    )
    assert router.record_audit(report(guidance="write report.csv"), 1, "cheap") is None
    assert router.record_audit(report(guidance="write report.csv"), 2, "cheap") is None
    escalation = router.record_audit(report(guidance="write report.csv"), 3, "cheap")
    assert escalation is not None
    assert escalation["reason"] == "no_progress_threshold_reached"
    assert escalation["stalled_rounds"] == 3
    assert escalation["failed_rounds"] == [1, 2, 3]


def test_a_changing_gap_resets_the_stall_streak():
    router = _ExecutorRouter(
        routing=ExecutorRouting(escalate_after_failures=0, escalate_after_stalled_rounds=3)
    )
    router.record_audit(report(guidance="step one"), 1, "cheap")
    router.record_audit(report(guidance="step one"), 2, "cheap")
    router.record_audit(report(guidance="a completely different obstacle"), 3, "cheap")
    assert router.stalled_rounds == 1
    assert router.record_audit(report(guidance="a completely different obstacle"), 4, "cheap") is None


def test_a_pass_clears_both_signals():
    router = _ExecutorRouter(routing=ExecutorRouting(escalate_after_stalled_rounds=2))
    router.record_audit(report(guidance="same"), 1, "cheap")
    router.record_audit(report(status="complete"), 2, "cheap")
    assert router.stalled_rounds == 0
    assert router.last_gap == ""


def test_the_stall_signal_can_be_disabled():
    router = _ExecutorRouter(
        routing=ExecutorRouting(escalate_after_failures=0, escalate_after_stalled_rounds=0)
    )
    for index in range(1, 8):
        assert router.record_audit(report(guidance="same gap"), index, "cheap") is None
    assert router.escalated is False


# --- through the loop -------------------------------------------------------


def test_an_exploration_round_does_not_escalate_the_run(harness_config):
    """The regression itself: round 1 is clean progress, round 2 must stay cheap."""
    config = harness_config(max_total_episodes=2, executor_routing=ExecutorRouting())
    run = run_loop(
        config,
        plans=[manager_plan("cli"), manager_plan("cli")],
        audits=[progress_report("create the file"), PASS],
    )
    assert run.tiers == ["cheap", "cheap"]
    assert executor_calls(run) == {"cli/cheap": 2}
    assert run.events("executor_escalation") == []


def test_a_genuinely_bad_round_still_escalates_the_run(harness_config):
    config = harness_config(max_total_episodes=2, executor_routing=ExecutorRouting())
    run = run_loop(config, plans=[manager_plan("cli")] * 2, audits=[PROBLEM, PASS])
    assert run.tiers == ["cheap", "strong"]
    assert run.events("executor_escalation")[0]["reason"] == "audit_failure_threshold_reached"


def test_a_stalled_run_escalates_through_the_loop(harness_config):
    config = harness_config(
        max_total_episodes=4,
        executor_routing=ExecutorRouting(escalate_after_stalled_rounds=3),
    )
    stalled = progress_report("write the summary file")
    run = run_loop(config, plans=[manager_plan("cli")] * 4, audits=[stalled] * 3 + [PASS])
    assert run.tiers == ["cheap", "cheap", "cheap", "strong"]
    escalation = run.events("executor_escalation")[0]
    assert escalation["reason"] == "no_progress_threshold_reached"
    assert escalation["round"] == 3


def test_the_stall_escalation_briefs_the_strong_executor(harness_config):
    config = harness_config(
        max_total_episodes=4,
        executor_routing=ExecutorRouting(escalate_after_stalled_rounds=3),
    )
    stalled = progress_report("write the summary file")
    run = run_loop(config, plans=[manager_plan("cli")] * 4, audits=[stalled] * 3 + [PASS])
    prompt = run.executor("cli", "strong").prompts[0]
    assert "Escalation briefing" in prompt
    assert "write the summary file" in prompt


def test_the_manager_notice_explains_which_signal_fired(harness_config):
    config = harness_config(
        max_total_episodes=4,
        executor_routing=ExecutorRouting(escalate_after_stalled_rounds=3),
    )
    stalled = progress_report("write the summary file")
    run = run_loop(config, plans=[manager_plan("cli")] * 4, audits=[stalled] * 3 + [PASS])
    notice = run.report["rounds"][2]["harness_feedback"]
    assert "no_progress_threshold_reached" in notice
    assert "same outstanding gap" in notice
