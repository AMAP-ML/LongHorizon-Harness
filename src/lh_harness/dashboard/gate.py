"""Single human-in-the-loop hook, evaluated at the END of every round.

The manager calls one hook per round with the round's ``outcome`` and
``reached_max``. The hook classifies whether a human gate is needed and, if so,
raises a blocking approval dialog. Trigger conditions (see ``_TRIGGERS``):

1. ``completed`` / ``max_rounds`` — the run finished or hit the round budget.
2. ``needs_human`` — the round's output explicitly requires human intervention
   (manager reported blocked).
3. ``repeated_failure`` — a special condition: too many failing rounds in a row.

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

# Shared option sets (buttons). ``continue`` keeps the run going, ``stop`` ends it.
_CONTINUE = ApprovalOption("continue", "继续运行", "primary")
_STOP_FINISH = ApprovalOption("stop", "结束", "danger")
_STOP_ABORT = ApprovalOption("stop", "终止", "danger")


@dataclass(frozen=True)
class _Trigger:
    """A dialog template for one gate condition (data-driven, easy to extend)."""

    title: str
    message: str
    options: list[ApprovalOption]
    input_label: str = "可选：补充指令，将注入下一轮任务管理"


_TRIGGERS: dict[str, _Trigger] = {
    "completed": _Trigger(
        "任务已完成，是否继续运行？",
        "任务管理器已确认任务完成。选择“继续运行”可追加轮次并注入指令，选择“结束”将停止本次运行。",
        [_CONTINUE, _STOP_FINISH],
    ),
    "max_rounds": _Trigger(
        "已到达最大轮次，是否继续运行？",
        "已用完预设的轮次预算但任务未完成。选择“继续运行”可追加轮次，选择“结束”将停止本次运行。",
        [_CONTINUE, _STOP_FINISH],
    ),
    "needs_input": _Trigger(
        "任务管理器请示：需要你的决定",
        "任务管理器需要你的决定或输入才能继续。请在下方输入你的回答，然后“继续运行”；或“终止”本次运行。",
        [_CONTINUE, _STOP_ABORT],
        input_label="你的回答（将作为指令注入下一轮任务管理）",
    ),
    "needs_human": _Trigger(
        "任务被阻塞，需要人工介入",
        "任务管理器判定当前无法自动推进（阻塞）。请补充指令后“继续运行”，或“终止”本次运行。",
        [_CONTINUE, _STOP_ABORT],
    ),
    "repeated_failure": _Trigger(
        "连续多轮未取得进展，请人工介入",
        "任务管理器连续多轮输出无效或完成请求被拒，可能陷入循环。请补充指令后“继续运行”，或“终止”。",
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
    if outcome == "blocked":
        return "needs_human", ""
    if reached_max:
        return "max_rounds", ""
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
            message = spec.message + "\n\n任务管理器的问题：\n" + question
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
                "task": context.get("task", ""),
                "task_state": context.get("task_state", ""),
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
