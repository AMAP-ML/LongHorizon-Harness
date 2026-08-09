"""Parsing the manager's optional `Executor tier:` line."""

from __future__ import annotations

import pytest

from lh_harness.role_prompts import (
    extract_role_manager_plan_text,
    extract_role_task_contract,
    extract_role_task_state,
    parse_role_manager_executor_tier,
    parse_role_manager_next_step,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Next: cli\nExecutor tier: cheap", "cheap"),
        ("Next: cli\nExecutor tier: strong", "strong"),
        ("Next: gui\n**Executor tier: strong**", "strong"),
        ("Next: cli\n  executor tier :  STRONG  ", "strong"),
        ("Next: cli\nExecutor tier: `cheap`", "cheap"),
        ("下一步: CLI任务\n执行器档位: strong", "strong"),
        ("下一步: GUI任务\n执行器档位：cheap", "cheap"),
        ("下一步: CLI任务\n执行档位: strong", "strong"),
    ],
)
def test_recognized_tiers(text, expected):
    assert parse_role_manager_executor_tier(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Next: cli\nTask: something",
        "Next: cli\nExecutor tier: medium",
        "Next: cli\nExecutor tier:",
        "Next: cli\nExecutor tier: cheap and strong",
        "",
    ],
)
def test_absent_or_unreadable_tier_is_none(text):
    """An unusable value must read as "no opinion", never raise."""
    assert parse_role_manager_executor_tier(text) is None


def test_tier_line_does_not_disturb_the_rest_of_the_protocol():
    plan = (
        "Current task state:\n"
        "Completed: nothing (round_002).\n"
        "\n"
        "Task contract:\n"
        "Persist the report.\n"
        "\n"
        "Next: cli\n"
        "Executor tier: strong\n"
        "\n"
        "Task: write the file\n"
    )
    assert parse_role_manager_next_step(plan) == "cli"
    assert extract_role_task_state(plan) == "Current task state:\nCompleted: nothing (round_002)."
    assert extract_role_task_contract(plan) == "Task contract:\nPersist the report."


def test_tier_survives_plan_extraction_from_a_noisy_transcript():
    raw = (
        "Let me think about this first.\n\n"
        "Current task state:\nCompleted: nothing.\n\n"
        "Next: cli\nExecutor tier: strong\n\nTask: go\n"
    )
    plan = extract_role_manager_plan_text(raw)
    assert parse_role_manager_executor_tier(plan) == "strong"
    assert "Let me think" not in plan
