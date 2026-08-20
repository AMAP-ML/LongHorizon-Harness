from __future__ import annotations

import pytest

from lh_harness.prompt_texts import AUDITOR_CONTRACT_BACKCHECK, TASK_CONTRACT_RULES
from lh_harness.role_prompts import (
    MANAGER_NEXT_CLI,
    MANAGER_NEXT_DONE,
    build_role_auditor_prompt,
    build_role_executor_prompt,
    build_role_final_response_prompt,
    build_role_manager_prompt,
    parse_role_manager_next_step,
)
from lh_harness.types import ManagedRound


@pytest.mark.parametrize("language", ["en", "zh"])
def test_auditor_prompt_does_not_turn_unobservable_history_into_a_blocker(language: str) -> None:
    contract_rules = TASK_CONTRACT_RULES[language]
    audit_rules = AUDITOR_CONTRACT_BACKCHECK[language]

    if language == "en":
        assert "does not silently become a historical guarantee" in contract_rules
        assert "missing full executor transcript is not by itself evidence" in audit_rules
        assert "unobservable historical negative is non-blocking residual risk" in audit_rules
        assert "Do not request a repeat solely" in audit_rules
    else:
        assert "不能被静默加强" in contract_rules
        assert "缺少执行器完整命令记录本身不是篡改证据" in audit_rules
        assert "事后不可观察的历史否定事实默认只是非阻断残余风险" in audit_rules
        assert "不得仅为证明已经不可观察" in audit_rules


@pytest.mark.parametrize("language", ["en", "zh"])
def test_final_response_prompt_preserves_operator_follow_up(language: str) -> None:
    prompt = build_role_final_response_prompt(
        task="Create the requested file.",
        rounds=[],
        status="complete",
        abort_reason="",
        task_state="The file was independently verified.",
        operator_instructions="The final reply must contain OPERATOR_MARKER.",
        language=language,
    )

    assert "OPERATOR_MARKER" in prompt
    if language == "en":
        assert "Authoritative operator follow-up instructions" in prompt
    else:
        assert "操作员后续补充的权威指令" in prompt


@pytest.mark.parametrize("language", ["en", "zh"])
def test_final_response_prompt_includes_verified_executor_deliverable(language: str) -> None:
    marker = "https://example.test/source METRIC=405K"
    prompt = build_role_final_response_prompt(
        task="Return the researched report with its citations.",
        rounds=[
            ManagedRound(
                round_index=1,
                next_step=MANAGER_NEXT_CLI,
                plan_text="research",
                executor_output=marker,
                auditor_report="Status: complete\nIntegrity: clean\nContract audit: aligned",
            )
        ],
        status="complete",
        abort_reason="",
        task_state="The report was verified.",
        language=language,
    )

    assert marker in prompt
    if language == "en":
        assert "preserve every source, citation, figure" in prompt
    else:
        assert "必须保留用户要求的全部来源、引用、数字" in prompt


@pytest.mark.parametrize(
    "line",
    [
        "Next: done — all constraints passed",
        "Next: done -- all constraints passed",
        "下一步: 完成（全部约束已满足）",
    ],
)
def test_manager_route_accepts_delimited_rationale(line: str) -> None:
    assert parse_role_manager_next_step(line) == MANAGER_NEXT_DONE


def test_manager_route_rejects_undelimited_suffix() -> None:
    assert parse_role_manager_next_step("Next: done later") != MANAGER_NEXT_DONE


@pytest.mark.parametrize(
    "line",
    [
        # The prompt shows every route inside backticks, and models copy that
        # formatting literally; bold was already accepted, so a code span must
        # not be the one markdown wrapper that voids an otherwise valid route.
        "`Next: cli`",
        "**`Next: cli`**",
        "`Next: cli` — routed to the CLI executor",
    ],
)
def test_manager_route_accepts_code_span_wrapping(line: str) -> None:
    assert parse_role_manager_next_step(line) == MANAGER_NEXT_CLI


@pytest.mark.parametrize("language", ["en", "zh"])
def test_manager_prompt_exposes_total_and_remaining_round_budget(language: str) -> None:
    prompt = build_role_manager_prompt(
        task="Research and compare the sources.",
        rounds=[],
        round_index=3,
        round_budget=10,
        language=language,
    )

    if language == "en":
        assert "Configured round limit: 10" in prompt
        assert "Rounds remaining, including this one: 8" in prompt
        assert "do not schedule a prerequisite-only subtask" in prompt
    else:
        assert "配置的轮次上限: 10" in prompt
        assert "包含本轮在内的剩余轮次: 8" in prompt
        assert "不得安排一个明确推迟核心要求的纯前置子任务" in prompt


def test_role_prompts_distinguish_workspace_from_run_records() -> None:
    executor = build_role_executor_prompt(
        task="create a report",
        plan_text="write report.md",
        next_step=MANAGER_NEXT_CLI,
        workspace_path="/tmp/workspace",
    )
    auditor = build_role_auditor_prompt(
        task="create a report",
        plan_text="write report.md",
        executor_output="done",
        next_step=MANAGER_NEXT_CLI,
        workspace_path="/tmp/workspace",
    )

    assert "Workspace root: /tmp/workspace" in executor
    assert "run-record directory" in executor
    assert "Real workspace root: /tmp/workspace" in auditor
    assert "Executor text is only a claim" in auditor
    assert "not require every subtask to create a file" in auditor
    assert "Private Dashboard trajectory images" in auditor
    assert ".longhorizon-evidence" not in auditor
