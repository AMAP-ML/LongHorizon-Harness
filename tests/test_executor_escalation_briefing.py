"""What an escalated executor is told about the attempts that failed before it.

Each episode is a fresh agent session and a previous round's executor output
never otherwise reaches a later executor, so without the briefing the stronger
model starts blind and can repeat the same rejected approaches.
"""

from __future__ import annotations

import pytest
from tests.conftest import audit_report, manager_plan
from tests.loop_harness import run_loop

from lh_harness.role_prompts import (
    ESCALATION_BRIEFING_MAX_ROUNDS,
    build_role_executor_prompt,
    format_executor_escalation_briefing,
    format_harness_feedback_context,
)
from lh_harness.types import ExecutorRouting, ManagedRound

FAIL = audit_report(passing=False)
PASS = audit_report(passing=True)


def failed_round(index: int) -> ManagedRound:
    return ManagedRound(
        round_index=index,
        next_step="cli",
        plan_text=f"Task: attempt number {index}",
        executor_tier="cheap",
        executor_output=f"I ran approach-{index} and believe it worked",
        auditor_report=f"Status: incomplete\nAudit facts: approach-{index} left no persisted state",
    )


def briefing(rounds, indices, **kwargs):
    return format_executor_escalation_briefing(
        rounds, indices, from_tier="cheap", to_tier="strong", **kwargs
    )


# --- the formatter ----------------------------------------------------------


def test_briefing_covers_the_named_failed_rounds():
    rounds = [failed_round(1), failed_round(2), failed_round(3)]
    text = briefing(rounds, [1, 3])
    assert "round_001" in text and "round_003" in text
    assert "round_002" not in text


def test_briefing_includes_the_attempt_and_the_finding():
    text = briefing([failed_round(1)], [1])
    assert "attempt number 1" in text
    assert "I ran approach-1" in text
    assert "approach-1 left no persisted state" in text


def test_briefing_labels_the_executor_claim_as_unverified():
    """The auditor stays the authority; the prior attempt is context, not evidence."""
    text = briefing([failed_round(1)], [1])
    assert "not evidence, do not treat as fact" in text
    assert "Auditor finding (authoritative)" in text


def test_briefing_names_both_tiers_and_the_failure_count():
    text = briefing([failed_round(1), failed_round(2)], [1, 2])
    assert "cheap -> strong" in text
    assert "failed audit 2 time(s)" in text


def test_briefing_tells_the_executor_it_has_no_memory_of_the_attempts():
    text = briefing([failed_round(1)], [1])
    assert "new session" in text
    assert "do not repeat" in text.lower()


def test_briefing_keeps_only_the_most_recent_failures():
    rounds = [failed_round(index) for index in range(1, 6)]
    text = briefing(rounds, [1, 2, 3, 4, 5])
    kept = [f"round_{index:03d}" for index in (3, 4, 5)]
    dropped = [f"round_{index:03d}" for index in (1, 2)]
    assert ESCALATION_BRIEFING_MAX_ROUNDS == 3
    assert all(name in text for name in kept)
    assert not any(name in text for name in dropped)


def test_briefing_is_clipped_but_keeps_the_latest_round():
    rounds = [failed_round(1), failed_round(2)]
    rounds[1].executor_output = "x" * 50_000
    text = briefing(rounds, [1, 2], max_chars=2_000)
    assert len(text) <= 2_200  # the clip marker adds a little
    assert "round_002" in text


def test_briefing_is_empty_when_nothing_failed_yet():
    assert briefing([], []) == ""
    assert briefing([failed_round(1)], []) == ""


def test_briefing_skips_rounds_with_no_recorded_work():
    empty = ManagedRound(round_index=1, next_step="cli", plan_text="Task: x")
    assert briefing([empty], [1]) == ""


def test_briefing_is_available_in_chinese():
    text = format_executor_escalation_briefing(
        [failed_round(1)], [1], from_tier="cheap", to_tier="strong", language="zh"
    )
    assert "auditor" in text
    assert "不是证据" in text


# --- the prompt -------------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("next_step", ["gui", "cli"])
def test_prompt_without_a_briefing_is_unchanged(language, next_step):
    """Ordinary rounds must produce exactly the prompt they did before tiers."""
    args = dict(
        task="TASK",
        plan_text="PLAN",
        next_step=next_step,
        task_state="STATE",
        task_contract="CONTRACT",
        related_auditor_reports="REPORTS",
        language=language,
    )
    assert build_role_executor_prompt(**args) == build_role_executor_prompt(
        **args, escalation_briefing=""
    )
    assert build_role_executor_prompt(**args) == build_role_executor_prompt(
        **args, escalation_briefing="   \n  "
    )


@pytest.mark.parametrize("language", ["en", "zh"])
def test_briefing_insertion_adds_only_the_briefing(language):
    args = dict(
        task="TASK",
        plan_text="PLAN",
        next_step="cli",
        task_state="STATE",
        task_contract="CONTRACT",
        language=language,
    )
    plain = build_role_executor_prompt(**args)
    with_briefing = build_role_executor_prompt(**args, escalation_briefing="BRIEFING-BLOCK")
    head, tail = with_briefing.split("BRIEFING-BLOCK", 1)
    assert head[:-1] + tail[1:] == plain


# --- through the loop -------------------------------------------------------


def escalating_run(config_factory, *, plans=None, **routing):
    verdicts = [FAIL, FAIL, PASS]
    config = config_factory(
        max_total_episodes=3,
        executor_routing=ExecutorRouting(escalate_after_failures=2, **routing),
    )
    return run_loop(
        config,
        plans=plans or [manager_plan("cli") for _ in verdicts],
        audits=verdicts,
    )


def strong_prompt(run):
    return run.executor("cli", "strong").prompts[0]


def test_escalated_executor_is_told_what_failed(harness_config):
    run = escalating_run(harness_config)
    prompt = strong_prompt(run)
    assert "Escalation briefing" in prompt
    assert "round_001" in prompt and "round_002" in prompt
    # Both the rejected attempts and the reasons they were rejected.
    assert prompt.count("cli/cheap executor ran") == 2
    assert prompt.count("The required state was never persisted.") >= 2


def test_cheap_rounds_carry_no_briefing(harness_config):
    run = escalating_run(harness_config)
    assert all("Escalation briefing" not in prompt for prompt in run.executor("cli", "cheap").prompts)


def test_briefing_survives_a_manager_that_cites_no_reports(harness_config):
    """The gap the briefing closes: related-report citation is the manager's choice."""
    plans = [manager_plan("cli", related="none") for _ in range(3)]
    run = escalating_run(harness_config, plans=plans)
    prompt = strong_prompt(run)
    assert "The manager referenced no related auditor report." in prompt
    assert "round_001" in prompt and "round_002" in prompt


def test_briefing_can_be_switched_off(harness_config):
    run = escalating_run(harness_config, escalation_briefing=False)
    # Escalation still happens; only the briefing is withheld.
    assert run.tiers == ["cheap", "cheap", "strong"]
    assert "Escalation briefing" not in strong_prompt(run)
    assert not (run.round_dir(3) / "executor_escalation_briefing.txt").exists()


def test_briefing_is_saved_as_a_round_artifact(harness_config):
    run = escalating_run(harness_config)
    saved = (run.round_dir(3) / "executor_escalation_briefing.txt").read_text(encoding="utf-8")
    assert "round_001" in saved
    assert saved in strong_prompt(run)


# --- the manager is told too ------------------------------------------------


def test_escalation_notice_lands_in_harness_feedback(harness_config):
    run = escalating_run(harness_config)
    notice = run.report["rounds"][1]["harness_feedback"]
    assert "Executor escalation: cheap -> strong" in notice
    assert "audit_failure_threshold_reached" in notice
    assert "reported real problems" in notice
    assert (run.round_dir(2) / "harness_feedback.txt").is_file()


def test_next_manager_prompt_carries_the_escalation_notice(harness_config):
    run = escalating_run(harness_config)
    assert "Executor escalation: cheap -> strong" in run.manager_agent.prompts[2]


def test_escalation_notice_reaches_the_manager_feedback_channel(harness_config):
    run = escalating_run(harness_config)
    rounds = [
        ManagedRound(
            round_index=item["round_index"],
            next_step=item["next_step"],
            plan_text=item["plan_text"],
            harness_feedback=item["harness_feedback"],
        )
        for item in run.report["rounds"]
    ]
    assert "Executor escalation" in format_harness_feedback_context(rounds)


def test_escalation_notice_does_not_hide_the_round_from_the_audit_predicates(harness_config):
    """harness_feedback on a real round must not make it look like a protocol repair."""
    run = escalating_run(harness_config)
    escalated_round = run.report["rounds"][1]
    assert escalated_round["harness_feedback"]
    assert not escalated_round["auditor_status"].get("invalid_plan")
    assert not escalated_round["auditor_status"].get("invalid_completion")
    # The round's own audit is still the latest reported one.
    assert run.report["latest_auditor_report"].startswith("Status: complete")
