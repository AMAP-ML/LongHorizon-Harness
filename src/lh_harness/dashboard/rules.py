"""Rule engine for the "repeated failure" human-review trigger.

Completion and max-rounds are decided by the manager directly; these rules
only cover the *special* condition, too many failing rounds in a row. Each rule
inspects the current round index plus recorded rounds and returns a
human-readable reason when human review should be requested, or ``None``.
"""

from __future__ import annotations

import os
from typing import Any, Callable

# A rule takes (round_index, rounds) and returns a reason or None.
Rule = Callable[[int, list[dict[str, Any]]], "str | None"]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def rule_repeated_failure(limit: int) -> Rule:
    """Ask for review after several consecutive failing rounds.

    A round counts as failing when the manager route was invalid, a
    completion request was rejected, or the task episode errored.
    """

    def _rule(round_index: int, rounds: list[dict[str, Any]]) -> str | None:
        del round_index
        streak = 0
        for item in reversed(rounds):
            auditor_status = item.get("auditor_status")
            auditor_status = auditor_status if isinstance(auditor_status, dict) else {}
            executor_status = item.get("executor_status")
            executor_status = executor_status if isinstance(executor_status, dict) else {}
            repair_status = auditor_status.get("format_repair_status")
            repair_status = repair_status if isinstance(repair_status, dict) else {}
            failed = bool(
                auditor_status.get("invalid_plan")
                or auditor_status.get("invalid_completion")
                or executor_status.get("status") in {"error", "timeout"}
                or auditor_status.get("status") in {"error", "timeout"}
                or repair_status.get("status") in {"error", "timeout"}
            )
            if failed:
                streak += 1
            else:
                break
        if streak >= limit:
            return (
                f"{streak} consecutive rounds failed (invalid route / rejected completion / "
                f"episode error; threshold {limit}). The run may be looping; operator input is requested."
            )
        return None

    return _rule


class ApprovalRules:
    """Aggregates the active rules and evaluates them for a round."""

    def __init__(self, rules: list[Rule] | None = None) -> None:
        if rules is None:
            rules = default_rules()
        self._rules = rules

    def evaluate(self, round_index: int, rounds: list[dict[str, Any]]) -> str | None:
        for rule in self._rules:
            reason = rule(round_index, rounds)
            if reason:
                return reason
        return None


def default_rules() -> list[Rule]:
    failure_limit = max(1, _env_int("LH_HARNESS_DASHBOARD_FAILURE_LIMIT", 3))
    return [rule_repeated_failure(failure_limit)]

