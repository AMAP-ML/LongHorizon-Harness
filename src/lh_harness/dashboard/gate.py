"""Single human-in-the-loop hook, evaluated at the END of every round.

The manager calls one hook per round with the round's ``outcome`` and
``reached_max``. The hook classifies whether a human gate is needed and, if so,
raises a blocking approval dialog. Trigger conditions (see ``_TRIGGERS``):

1. ``completed`` / ``max_rounds``: the run finished or hit the round budget.
2. ``needs_human``: the round's output explicitly requires human intervention
   (manager reported blocked).
3. ``repeated_failure``, a special condition: too many failing rounds in a row.

Adding a new trigger = add one ``_Trigger`` to ``_TRIGGERS`` and one clause in
``_classify``; nothing else changes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .rules import ApprovalRules
from .state import ApprovalOption, DashboardState

HumanHook = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

# Dialog text is authored in English and persisted that way in approvals.jsonl.
# The dashboard localizes each dialog from its `trigger` key, so these strings
# are the fallback for clients that do not (older records, API consumers).
_CONTINUE = ApprovalOption("continue", "Continue run", "primary")
_STOP_FINISH = ApprovalOption("stop", "End run", "danger")
_STOP_ABORT = ApprovalOption("stop", "Stop run", "danger")


@dataclass(frozen=True)
class _Trigger:
    """A dialog template for one gate condition (data-driven, easy to extend)."""

    title: str
    message: str
    options: list[ApprovalOption]
    input_label: str = "Optional: add instructions for the next manager round"


_TRIGGERS: dict[str, _Trigger] = {
    "completed": _Trigger(
        "Task complete. Continue the run?",
        "The manager confirmed task completion. Continue to add rounds and inject instructions, or end this run.",
        [_CONTINUE, _STOP_FINISH],
    ),
    "max_rounds": _Trigger(
        "Round limit reached. Continue the run?",
        "The configured round budget is exhausted before completion. Continue to add rounds, or end this run.",
        [_CONTINUE, _STOP_FINISH],
    ),
    "needs_input": _Trigger(
        "Manager needs your decision",
        "The manager needs your decision or input before it can continue. Answer below and continue, or stop this run.",
        [_CONTINUE, _STOP_ABORT],
        input_label="Your answer, injected into the next manager round",
    ),
    "needs_human": _Trigger(
        "Task blocked; operator input required",
        "The manager reported that it cannot proceed automatically. Add instructions and continue, or stop this run.",
        [_CONTINUE, _STOP_ABORT],
    ),
    "repeated_failure": _Trigger(
        "Repeated failures require operator input",
        "The manager produced invalid routes or rejected completions over several rounds and may be looping. Add instructions and continue, or stop this run.",
        [_CONTINUE, _STOP_ABORT],
    ),
}


def _classify(
    outcome: str,
    reached_max: bool,
    round_index: int,
    rounds: list[dict[str, Any]],
    rules: ApprovalRules,
) -> tuple[str, str] | None:
    """Return ``(trigger_kind, extra_message)`` when a gate is needed, else None."""
    if outcome == "completed":
        return "completed", ""
    if outcome == "ask":
        return "needs_input", ""  # extra_message filled from the manager question
    # The hard round limit takes precedence over a generic blocked/failure
    # outcome on the same final round, so the operator is explicitly told that
    # the configured budget was exhausted and can decide whether to extend it.
    if reached_max:
        return "max_rounds", ""
    if outcome == "blocked":
        return "needs_human", ""
    reason = rules.evaluate(round_index, rounds)  # repeated-failure streak, etc.
    if reason:
        return "repeated_failure", reason
    return None


def make_human_hook(
    state: DashboardState,
    *,
    rules: ApprovalRules | None = None,
    poll_interval: float = 0.5,
    default_extra_rounds: int = 0,
) -> HumanHook:
    """Build the unified end-of-round human-in-the-loop hook for the dashboard."""
    rules = rules or ApprovalRules()

    async def _wait_resolved(approval_id: str):
        while True:
            current = state.get_approval(approval_id)
            if current is not None and current.status == "resolved":
                return current
            await asyncio.sleep(poll_interval)

    async def _hook(context: dict[str, Any]) -> dict[str, Any]:
        # Free-form operator notes queued from the UI are always carried forward.
        injections = [text for text in state.drain_injections() if text.strip()]

        outcome = str(context.get("outcome") or "progress")
        reached_max = bool(context.get("reached_max"))
        round_index = int(context.get("round_index", 0))
        rounds = context.get("rounds") or []

        classified = _classify(outcome, reached_max, round_index, rounds, rules)
        if classified is None:
            # No blocking gate this round; just pass queued injections forward.
            return {"action": "continue", "instructions": "\n".join(injections)}

        kind, extra_message = classified
        spec = _TRIGGERS[kind]
        # For an "ask" gate, show the manager's actual question prominently
        # and offer its quick-answer choices (e.g. 是/否) as one-click buttons.
        question = str(context.get("question") or "").strip()
        answers = context.get("answers") if kind == "needs_input" else None
        answers = [str(a) for a in answers] if isinstance(answers, list) else []
        if kind == "needs_input" and question:
            message = spec.message + "\n\nManager question:\n" + question
        else:
            message = extra_message or spec.message
        approval = state.create_approval(
            title=spec.title,
            message=message,
            options=list(spec.options),
            answers=answers,
            input_label=spec.input_label,
            context={
                "phase": "end_of_round",
                "trigger": kind,
                "outcome": outcome,
                "round_index": round_index,
                "question": question,
                # Kept separate from `message`: clients localize the dialog from
                # `trigger`, which would otherwise discard this rule detail.
                "detail": extra_message,
                "task": context.get("task", ""),
                "task_state": context.get("task_state", ""),
                # The reply is written before this gate so the operator decides
                # against the actual answer; report.json does not exist yet.
                "final_response": context.get("final_response", ""),
                "round_count": len(rounds),
            },
        )
        resolved = await _wait_resolved(approval.approval_id)
        parts = list(injections)
        if resolved.user_input.strip():
            parts.append(resolved.user_input.strip())
        return {
            "action": resolved.action,  # "continue" | "stop"
            "instructions": "\n".join(parts),
            "extra_rounds": default_extra_rounds,  # 0 -> manager default budget
            "reason": resolved.reason,
        }

    return _hook
