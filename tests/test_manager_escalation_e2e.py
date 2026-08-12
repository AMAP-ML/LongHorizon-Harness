"""The whole escalation flow through one real `manager.run` call.

Manager creates a task -> cheap executor -> audit fails -> retry on cheap ->
audit fails -> threshold reached -> strong executor -> audit passes -> the
manager's completion claim is accepted.

This deliberately raises `escalate_after_failures` to 2 so the flow includes a
same-tier retry round. The shipped default is 1, covered by
`test_the_default_escalates_on_the_first_failed_audit`.
"""

from __future__ import annotations

import pytest
from tests.conftest import audit_report, manager_plan
from tests.loop_harness import executor_calls, run_loop

from lh_harness.dashboard.state import DashboardState
from lh_harness.types import ExecutorRouting

FAIL = audit_report(passing=False, detail="The report file was never written to disk.")
PASS = audit_report(passing=True, detail="report.csv exists with the required rows.")


@pytest.fixture
def flow(harness_config):
    config = harness_config(
        max_total_episodes=4,
        executor_routing=ExecutorRouting(
            default_tier="cheap", escalate_after_failures=2, escalation_tier="strong"
        ),
    )
    return run_loop(
        config,
        task="Produce report.csv from the source data",
        plans=[
            manager_plan("cli", task="Write report.csv"),
            manager_plan("cli", task="Write report.csv, properly this time"),
            manager_plan("cli", task="Write report.csv"),
            manager_plan("done"),
        ],
        audits=[FAIL, FAIL, PASS],
    )


def test_the_run_completes(flow):
    assert flow.report["status"] == "complete"
    assert flow.report["completion_satisfied"] is True
    assert flow.report["rounds_run"] == 4


def test_execution_moves_from_cheap_to_strong_at_the_threshold(flow):
    assert flow.tiers == ["cheap", "cheap", "strong", ""]
    assert executor_calls(flow) == {"cli/cheap": 2, "cli/strong": 1}


def test_the_strong_round_is_what_unlocked_completion(flow):
    """Verification was not bypassed: the escalated work was audited and passed."""
    assert flow.auditors["cli"].calls == 3
    assert flow.report["rounds"][2]["executor_tier"] == "strong"
    assert flow.report["rounds"][2]["auditor_report"].startswith("Status: complete")
    assert flow.report["latest_auditor_report"].startswith("Status: complete")


def test_the_strong_executor_knew_why_the_cheap_one_failed(flow):
    prompt = flow.executor("cli", "strong").prompts[0]
    assert "Escalation briefing" in prompt
    assert "round_001" in prompt and "round_002" in prompt
    assert "The report file was never written to disk." in prompt


def test_the_escalation_is_traceable_in_the_event_stream(flow):
    escalations = flow.events("executor_escalation")
    assert len(escalations) == 1
    assert escalations[0]["round"] == 2
    assert escalations[0]["failed_rounds"] == [1, 2]

    starts = flow.events("executor_role_start")
    assert [item["executor_tier"] for item in starts] == ["cheap", "cheap", "strong"]
    assert [item["tier_source"] for item in starts] == ["default", "default", "escalated"]
    assert starts[2]["escalation_briefing_chars"] > 0


def test_the_persisted_round_ledger_records_each_tier(flow):
    assert [item["executor_tier"] for item in flow.rounds_jsonl()] == ["cheap", "cheap", "strong", ""]


def test_the_report_summarizes_what_routing_did(flow):
    routing = flow.report["executor_routing"]
    assert routing["default_tier"] == "cheap"
    assert routing["escalation_tier"] == "strong"
    assert routing["escalate_after_failures"] == 2
    assert routing["rounds_by_tier"] == {"cheap": 2, "strong": 1}
    # The run recovered, so it is no longer escalated, but the report still
    # records that it escalated once and why.
    assert routing["escalated"] is False
    assert len(routing["escalations"]) == 1
    assert routing["escalations"][0]["round"] == 2
    assert routing["escalations"][0]["from_tier"] == "cheap"
    assert routing["escalations"][0]["to_tier"] == "strong"
    assert routing["escalations"][0]["reason"] == "audit_failure_threshold_reached"


def test_retry_counts_stay_consistent_with_the_tiers(flow):
    """Two failures on cheap, then a pass on strong that clears the counter."""
    assert flow.report["executor_routing"]["consecutive_audit_failures"] == 0
    assert flow.events("executor_escalation")[0]["consecutive_audit_failures"] == 2


def test_the_dashboard_reports_the_tier_for_every_round(flow):
    state = DashboardState(str(flow.log_dir))
    tiers = {item["round_index"]: item.get("executor_tier") for item in state.read_rounds()}
    assert tiers[1] == "cheap"
    assert tiers[2] == "cheap"
    assert tiers[3] == "strong"


def test_the_dashboard_shows_the_tier_before_a_round_finishes(flow):
    """`executor_tier.txt` is written before the executor runs, so live rounds show it."""
    state = DashboardState(str(flow.log_dir))
    (flow.log_dir / "role_orchestration" / "rounds.jsonl").unlink()
    (flow.log_dir / "report.json").unlink()
    (flow.log_dir / "role_orchestration" / "report.json").unlink()
    tiers = {item["round_index"]: item.get("executor_tier") for item in state.read_rounds()}
    assert tiers[3] == "strong"


def test_the_briefing_is_archived_next_to_the_round_it_was_used_in(flow):
    """On disk, because the dashboard's artifact listing is POSIX-only today."""
    assert (flow.round_dir(3) / "executor_escalation_briefing.txt").is_file()
    assert (flow.round_dir(3) / "executor_tier.txt").is_file()
    assert not (flow.round_dir(1) / "executor_escalation_briefing.txt").exists()
