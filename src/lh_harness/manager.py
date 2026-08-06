from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .adapters.base import AgentAdapter
from .agent_logs import visible_output as decode_agent_visible_output
from .environment.base import Environment
from .environment.remote_files import ensure_remote_dir, write_remote_text
from .runtime_signals import hard_signal_labels
from .role_prompts import (
    MANAGER_NEXT_BLOCKED,
    MANAGER_NEXT_DONE,
    MANAGER_NEXT_GUI,
    MANAGER_NEXT_INVALID,
    MANAGER_NEXT_ASK,
    build_role_manager_prompt,
    build_role_executor_prompt,
    build_role_auditor_format_repair_prompt,
    build_role_auditor_prompt,
    extract_role_manager_plan_text,
    extract_related_report_refs,
    extract_role_manager_answer_choices,
    extract_role_manager_question,
    extract_role_task_contract,
    extract_role_task_state,
    format_related_auditor_reports,
    format_management_history,
    parse_role_manager_next_step,
)
from .types import (
    EpisodeBudget,
    EpisodeResult,
    HarnessConfig,
    ManagedRound,
    RoleNextStep,
)
from .auditor_agent import (
    VISIBLE_OUTPUT_KEYS,
    has_valid_auditor_control_header,
    parse_audit_report,
    auditor_report_text_from_episode_result,
    audit_report_from_episode_result,
)

ROLE_VARIANT = "lh_harness_role_managed"
logger = logging.getLogger(__name__)


def _invalid_completion_feedback(language: str) -> str:
    if language == "en":
        return (
            "Status: incomplete\n"
            "Integrity: suspect\n"
            "Contract audit: unknown\n"
            "Audit facts: the manager requested completion, but the latest auditor report did not confirm every original requirement as complete with clean integrity and an aligned contract.\n"
            "Gap: schedule an auditable GUI/CLI subtask or obtain an explicit auditor confirmation.\n"
            "Next step: manage again; do not emit `Next: done` without complete/clean/aligned evidence."
        )
    return (
        "状态: incomplete\n"
        "完整性: suspect\n"
        "契约审计: unknown\n"
        "审计事实: 任务管理器请求完成，但最近 auditor 报告没有明确确认所有原始要求 complete、clean 且契约 aligned。\n"
        "缺口: 必须先分配一个可审计的 GUI/CLI 子任务，或等待 auditor 明确确认完成。\n"
        "下一步: 重新任务管理；没有 complete/clean/aligned 证据时不能输出 `下一步: 完成`。"
    )


def _invalid_plan_feedback(language: str) -> str:
    if language == "en":
        return (
            "Status: incomplete\n"
            "Integrity: suspect\n"
            "Contract audit: unknown\n"
            "Audit facts: the manager output did not contain a valid route, so no GUI or CLI executor can be assigned.\n"
            "Gap: emit one dominant GUI/CLI subtask or an explicit ask/done/blocked route.\n"
            "Next step: manage again using exactly `Next: gui`, `Next: cli`, `Next: ask`, `Next: done`, or `Next: blocked`."
        )
    return (
        "状态: incomplete\n"
        "完整性: suspect\n"
        "契约审计: unknown\n"
        "审计事实: 任务管理器输出没有有效路由，无法分配 GUI 或 CLI executor。\n"
        "缺口: 输出一个主目标明确的 GUI/CLI 子任务，或明确请示用户/完成/阻塞。\n"
        "下一步: 使用 `下一步: GUI任务`、`下一步: CLI任务`、`下一步: 请示用户`、`下一步: 完成` 或 `下一步: 阻塞` 重新管理。"
    )


async def run(
    *,
    task: str,
    env: Environment,
    config: HarnessConfig,
    agent: AgentAdapter | None = None,
    auditor_agent: AgentAdapter | None = None,
    manager_agent: AgentAdapter | None = None,
    gui_executor_agent: AgentAdapter | None = None,
    cli_executor_agent: AgentAdapter | None = None,
    gui_auditor_agent: AgentAdapter | None = None,
    cli_auditor_agent: AgentAdapter | None = None,
    auditor_format_repair_agent: AgentAdapter | None = None,
    human_hook: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Run the generic LongHorizon-Harness four-role management loop.

    The default `agent` can back every role, which is how Codex or Claude Code
    adapters start. Callers with stronger role controls can pass distinct
    adapters for manager, GUI task, CLI task, GUI auditor, and CLI auditor.

    ``human_hook`` is a single optional human-in-the-loop callback (used by the
    dashboard for approval / instruction injection). It runs at the END of every
    round with ``context`` describing that round's outcome::

        {"phase": "end_of_round",
         "outcome": "completed" | "blocked" | "progress",
         "reached_max": bool, "round_index": int,
         "task", "task_state", "rounds", "log_dir"}

    The hook decides whether a human gate is needed (task completed, max rounds
    reached, manager blocked, or repeated failures) and returns
    ``{"action": "continue" | "stop", "instructions": str, "extra_rounds": int}``.
    ``"continue"`` keeps the run going (reopening / extending the budget when it
    was about to finish, and injecting any instructions); ``"stop"`` ends the
    run. Queued non-blocking operator instructions are drained here too.
    """

    # Role binding is resolved once at startup so the main loop can stay focused
    # on state transitions instead of adapter fallback logic.
    manager_agent = manager_agent or agent
    gui_executor_agent = gui_executor_agent or agent
    cli_executor_agent = cli_executor_agent or agent
    gui_auditor_agent = gui_auditor_agent or auditor_agent or agent
    cli_auditor_agent = cli_auditor_agent or auditor_agent or agent
    if any(
        role_agent is None
        for role_agent in (
            manager_agent,
            gui_executor_agent,
            cli_executor_agent,
            gui_auditor_agent,
            cli_auditor_agent,
        )
    ):
        raise ValueError("Every role needs an agent, or a default agent must be provided")

    # Every role reads one explicit budget. Keeping the resolved budgets in the
    # config avoids the previous episode/auditor alias chain, where duplicate
    # fields made it unclear which timeout values actually won.
    manager_budget = config.manager_budget
    gui_executor_budget = config.gui_executor_budget
    cli_executor_budget = config.cli_executor_budget
    auditor_budget = config.auditor_budget

    log_dir = Path(config.log_dir)
    role_dir = log_dir / "role_management"
    rounds_dir = role_dir / "rounds"
    rounds_dir.mkdir(parents=True, exist_ok=True)
    events_path = role_dir / "events.jsonl"
    started = time.monotonic()

    await _ensure_remote_layout(env, config)
    _append_event(
        events_path,
        "role_harness_start",
        {
            "variant": ROLE_VARIANT,
            "task_chars": len(task),
            "workspace_path": config.workspace_path,
            "harness_dir": config.harness_dir,
            "max_rounds": config.max_total_episodes,
            "manager_budget": _budget_to_dict(manager_budget),
            "gui_executor_budget": _budget_to_dict(gui_executor_budget),
            "cli_executor_budget": _budget_to_dict(cli_executor_budget),
            "auditor_budget": _budget_to_dict(auditor_budget),
        },
    )

    rounds: list[ManagedRound] = []
    last_plan = ""
    current_task_state = ""
    current_task_contract = ""
    round_index = 0

    # The gate context bundles run-scoped dependencies with the loop state the
    # end-of-round human gate updates (round budget, completion, abort reason,
    # carryover instructions). The loop calls one module-level gate function
    # directly and reads the results straight back from `gate`.
    gate = _GateContext(
        human_hook=human_hook,
        task=task,
        rounds=rounds,
        log_dir=log_dir,
        config=config,
        events_path=events_path,
        round_budget=max(1, config.max_total_episodes),
    )

    while round_index < gate.round_budget:
        round_index += 1
        round_dir = rounds_dir / f"round_{round_index:03d}"
        round_dir.mkdir(parents=True, exist_ok=True)

        # The manager sees the original task, its maintained task state,
        # and auditor reports. It never receives raw trajectories or previous
        # full prompts.
        manager_prompt = build_role_manager_prompt(
            task=task,
            rounds=rounds,
            round_index=round_index,
            task_state=current_task_state,
            task_contract=current_task_contract,
            language=config.prompt_language,
            max_history_chars=config.role_history_chars,
        )

        # Instructions carried over from the end-of-round human gate (queued
        # operator notes and/or an approval's free-form input) are injected into
        # this round's manager prompt.
        if gate.carryover_instructions:
            instruction_heading = (
                "Operator instructions injected through the dashboard (high priority; incorporate them this round):"
                if config.prompt_language == "en"
                else "人工补充指令（操作员通过 dashboard 注入，优先级高，请纳入本轮任务管理）:"
            )
            manager_prompt += f"\n\n{instruction_heading}\n{gate.carryover_instructions}\n"
            _write_local(round_dir / "human_instructions.txt", gate.carryover_instructions)
            _append_event(
                events_path,
                "human_instructions_injected",
                {"round": round_index, "chars": len(gate.carryover_instructions)},
            )
            gate.carryover_instructions = ""

        _write_local(round_dir / "manager_input.txt", manager_prompt)
        await _write_remote_round_text(env, config, round_index, "manager_input.txt", manager_prompt)
        _append_event(
            events_path,
            "manager_round_start",
            {"round": round_index, "prompt_chars": len(manager_prompt)},
        )

        manager_result = await _run_role_episode(
            manager_agent,
            manager_prompt,
            env,
            manager_budget,
            live_trajectory_path=str(round_dir / "manager_raw_trajectory.jsonl"),
        )
        _save_role_result(round_dir, "manager", manager_result)
        if manager_result.status == "cancelled":
            gate.abort_reason = "user_cancelled"
            _append_event(
                events_path,
                "role_harness_cancelled",
                {"round": round_index, "phase": "manager", "status": _episode_status(manager_result)},
            )
            break
        plan_text = extract_role_manager_plan_text(_visible_output(manager_result)).strip()
        if not plan_text:
            plan_text = (
                "Next: blocked\n\nReason:\nThe manager produced no readable natural-language output."
                if config.prompt_language == "en"
                else "下一步: 阻塞\n\n阻塞原因:\n任务管理器没有产生可读取的自然语言输出。"
            )
        current_task_state = extract_role_task_state(plan_text, fallback=current_task_state)
        current_task_contract = extract_role_task_contract(plan_text, fallback=current_task_contract)
        related_report_refs = extract_related_report_refs(plan_text)
        _write_local(round_dir / "manager_plan.txt", plan_text)
        _write_local(round_dir / "task_state.txt", current_task_state)
        _write_local(round_dir / "task_contract.txt", current_task_contract)
        await _write_remote_round_text(env, config, round_index, "manager_plan.txt", plan_text)
        await _write_remote_round_text(env, config, round_index, "task_state.txt", current_task_state)
        await _write_remote_round_text(env, config, round_index, "task_contract.txt", current_task_contract)

        next_step = parse_role_manager_next_step(plan_text)
        last_plan = plan_text
        _append_event(
            events_path,
            "manager_round_done",
            {
                "round": round_index,
                "next_step": next_step,
                "plan_chars": len(plan_text),
                "task_state_chars": len(current_task_state),
                "task_contract_chars": len(current_task_contract),
                "related_report_refs": related_report_refs,
                "status": _episode_status(manager_result),
            },
        )

        if next_step == MANAGER_NEXT_DONE:
            if _latest_auditor_is_clean_complete(rounds, language=config.prompt_language):
                gate.completion_satisfied = True
                rounds.append(
                    ManagedRound(
                        round_index=round_index,
                        next_step=next_step,
                        plan_text=plan_text,
                        task_state=current_task_state,
                        task_contract=current_task_contract,
                        related_report_refs=related_report_refs,
                    )
                )
                await _record_round(env, config, role_dir, events_path, rounds[-1])
                if await _human_gate(gate, "completed", round_index, current_task_state):
                    break
                continue

            # Completion is not accepted unless it is grounded in a previous
            # clean auditor report. The synthetic audit gets fed back into the
            # next manager turn as a repair signal.
            repair_report = _invalid_completion_feedback(config.prompt_language)
            record = ManagedRound(
                round_index=round_index,
                next_step=MANAGER_NEXT_INVALID,
                plan_text=plan_text,
                harness_feedback=repair_report,
                task_state=current_task_state,
                task_contract=current_task_contract,
                related_report_refs=related_report_refs,
                auditor_status={"invalid_completion": True},
            )
            _write_local(round_dir / "harness_feedback.txt", repair_report)
            await _write_remote_round_text(env, config, round_index, "harness_feedback.txt", repair_report)
            rounds.append(record)
            await _record_round(env, config, role_dir, events_path, record)
            if await _human_gate(gate, "progress", round_index, current_task_state):
                break
            continue

        if next_step == MANAGER_NEXT_BLOCKED:
            rounds.append(
                ManagedRound(
                    round_index=round_index,
                    next_step=next_step,
                    plan_text=plan_text,
                    task_state=current_task_state,
                    task_contract=current_task_contract,
                    related_report_refs=related_report_refs,
                )
            )
            await _record_round(env, config, role_dir, events_path, rounds[-1])
            if await _human_gate(gate, "blocked", round_index, current_task_state):
                break
            continue

        if next_step == MANAGER_NEXT_ASK:
            # The manager needs a human decision/input to proceed (e.g. the
            # task says "ask me next step"). This is a harness-level gate, not a
            # subtask: record the round and raise a human dialog with the
            # manager's question; the answer is injected into the next round.
            question = extract_role_manager_question(plan_text)
            answers = extract_role_manager_answer_choices(plan_text)
            rounds.append(
                ManagedRound(
                    round_index=round_index,
                    next_step=next_step,
                    plan_text=plan_text,
                    task_state=current_task_state,
                    task_contract=current_task_contract,
                    related_report_refs=related_report_refs,
                )
            )
            await _record_round(env, config, role_dir, events_path, rounds[-1])
            if await _human_gate(gate, "ask", round_index, current_task_state, question=question, answers=answers):
                break
            continue

        if next_step == MANAGER_NEXT_INVALID:
            # Bad route output is treated like a auditor finding so the next
            # manager turn has an explicit, auditable correction signal.
            repair_report = _invalid_plan_feedback(config.prompt_language)
            record = ManagedRound(
                round_index=round_index,
                next_step=MANAGER_NEXT_INVALID,
                plan_text=plan_text,
                harness_feedback=repair_report,
                task_state=current_task_state,
                task_contract=current_task_contract,
                related_report_refs=related_report_refs,
                auditor_status={"invalid_plan": True},
            )
            _write_local(round_dir / "harness_feedback.txt", repair_report)
            await _write_remote_round_text(env, config, round_index, "harness_feedback.txt", repair_report)
            rounds.append(record)
            await _record_round(env, config, role_dir, events_path, record)
            if await _human_gate(gate, "progress", round_index, current_task_state):
                break
            continue

        executor_agent, executor_budget = _executor_binding(
            next_step=next_step,
            gui_executor_agent=gui_executor_agent,
            cli_executor_agent=cli_executor_agent,
            gui_executor_budget=gui_executor_budget,
            cli_executor_budget=cli_executor_budget,
        )
        auditor_for_step = gui_auditor_agent if next_step == MANAGER_NEXT_GUI else cli_auditor_agent
        related_auditor_reports = format_related_auditor_reports(
            rounds,
            related_report_refs,
            max_chars=config.role_verified_context_chars,
            language=config.prompt_language,
        )

        # Task prompts receive the manager-maintained state plus only the
        # auditor reports explicitly referenced by the current subtask contract.
        executor_prompt = build_role_executor_prompt(
            task=task,
            plan_text=plan_text,
            next_step=next_step,
            task_state=current_task_state,
            task_contract=current_task_contract,
            related_auditor_reports=related_auditor_reports,
            language=config.prompt_language,
        )
        _write_local(round_dir / "executor_prompt.txt", executor_prompt)
        await _write_remote_round_text(env, config, round_index, "executor_prompt.txt", executor_prompt)
        _append_event(
            events_path,
            "executor_role_start",
            {"round": round_index, "role": next_step, "prompt_chars": len(executor_prompt), "budget": _budget_to_dict(executor_budget)},
        )

        executor_result = await _run_role_episode(
            executor_agent,
            executor_prompt,
            env,
            executor_budget,
            live_trajectory_path=str(round_dir / "executor_raw_trajectory.jsonl"),
        )
        _save_role_result(round_dir, "executor", executor_result)
        executor_output = _visible_output(executor_result).strip() or "(executor agent produced no readable natural-language output)"
        _write_local(round_dir / "executor_output.txt", executor_output)
        if executor_result.status == "cancelled":
            record = ManagedRound(
                round_index=round_index,
                next_step=next_step,
                plan_text=plan_text,
                executor_output=executor_output,
                task_state=current_task_state,
                task_contract=current_task_contract,
                related_report_refs=related_report_refs,
                executor_status=_episode_status(executor_result),
            )
            rounds.append(record)
            await _record_round(env, config, role_dir, events_path, record)
            gate.abort_reason = "user_cancelled"
            _append_event(
                events_path,
                "role_harness_cancelled",
                {"round": round_index, "phase": "executor", "status": _episode_status(executor_result)},
            )
            break
        await _write_remote_round_text(env, config, round_index, "executor_output.txt", executor_output)
        _append_event(
            events_path,
            "executor_role_done",
            {
                "round": round_index,
                "role": next_step,
                "output_chars": len(executor_output),
                "status": _episode_status(executor_result),
            },
        )

        # The auditor audits only the just-finished subtask. Its natural
        # language report becomes the trusted intermediate state for later rounds.
        auditor_prompt = build_role_auditor_prompt(
            task=task,
            plan_text=plan_text,
            executor_output=executor_output,
            next_step=next_step,
            task_state=current_task_state,
            task_contract=current_task_contract,
            related_auditor_reports=related_auditor_reports,
            max_executor_output_chars=config.auditor_output_chars,
            language=config.prompt_language,
        )
        _write_local(round_dir / "auditor_input.txt", auditor_prompt)
        await _write_remote_round_text(env, config, round_index, "auditor_input.txt", auditor_prompt)
        _append_event(
            events_path,
            "auditor_role_start",
            {"round": round_index, "role": next_step, "prompt_chars": len(auditor_prompt), "budget": _budget_to_dict(auditor_budget)},
        )

        auditor_result = await _run_role_episode(
            auditor_for_step,
            auditor_prompt,
            env,
            auditor_budget,
            live_trajectory_path=str(round_dir / "auditor_raw_trajectory.jsonl"),
        )
        _save_role_result(round_dir, "auditor", auditor_result)
        if auditor_result.status == "cancelled":
            record = ManagedRound(
                round_index=round_index,
                next_step=next_step,
                plan_text=plan_text,
                executor_output=executor_output,
                task_state=current_task_state,
                task_contract=current_task_contract,
                related_report_refs=related_report_refs,
                executor_status=_episode_status(executor_result),
                auditor_status=_episode_status(auditor_result),
            )
            rounds.append(record)
            await _record_round(env, config, role_dir, events_path, record)
            gate.abort_reason = "user_cancelled"
            _append_event(
                events_path,
                "role_harness_cancelled",
                {"round": round_index, "phase": "auditor", "status": _episode_status(auditor_result)},
            )
            break
        auditor_report, auditor_status = await _auditor_report_with_format_repair(
            env=env,
            config=config,
            round_dir=round_dir,
            events_path=events_path,
            # By default, repair uses the same concrete GUI/CLI auditor that
            # produced the report. Callers may still provide an explicit
            # override for compatibility or backend specialization.
            format_repair_agent=auditor_format_repair_agent or auditor_for_step,
            auditor_budget=auditor_budget,
            primary_result=auditor_result,
            round_index=round_index,
        )
        repair_status = auditor_status.get("format_repair_status")
        if isinstance(repair_status, dict) and repair_status.get("status") == "cancelled":
            record = ManagedRound(
                round_index=round_index,
                next_step=next_step,
                plan_text=plan_text,
                executor_output=executor_output,
                task_state=current_task_state,
                task_contract=current_task_contract,
                related_report_refs=related_report_refs,
                executor_status=_episode_status(executor_result),
                auditor_status=auditor_status,
            )
            rounds.append(record)
            await _record_round(env, config, role_dir, events_path, record)
            gate.abort_reason = "user_cancelled"
            _append_event(
                events_path,
                "role_harness_cancelled",
                {"round": round_index, "phase": "auditor_format_repair", "status": repair_status},
            )
            break
        _write_local(round_dir / "auditor_report.txt", auditor_report)
        await _write_remote_round_text(env, config, round_index, "auditor_report.txt", auditor_report)

        record = ManagedRound(
            round_index=round_index,
            next_step=next_step,
            plan_text=plan_text,
            executor_output=executor_output,
            auditor_report=auditor_report,
            task_state=current_task_state,
            task_contract=current_task_contract,
            related_report_refs=related_report_refs,
            executor_status=_episode_status(executor_result),
            auditor_status=auditor_status,
        )
        rounds.append(record)
        await _record_round(env, config, role_dir, events_path, record)
        _append_event(
            events_path,
            "auditor_role_done",
            {
                "round": round_index,
                "role": next_step,
                "report_chars": len(auditor_report),
                "status": _episode_status(auditor_result),
            },
        )
        if await _human_gate(gate, "progress", round_index, current_task_state):
            break

    elapsed = time.monotonic() - started
    final = _final_report(
        task=task,
        rounds=rounds,
        completion_satisfied=gate.completion_satisfied,
        abort_reason=gate.abort_reason,
        last_plan=last_plan,
        task_state=current_task_state,
        task_contract=current_task_contract,
        max_rounds=max(1, config.max_total_episodes),
        elapsed_seconds=elapsed,
    )
    _write_local(role_dir / "report.json", json.dumps(final, ensure_ascii=False, indent=2) + "\n")
    _write_local(log_dir / "report.json", json.dumps(final, ensure_ascii=False, indent=2) + "\n")
    transcript = format_management_history(rounds, include_empty=True, max_chars=200_000)
    _write_local(role_dir / "management_transcript.txt", transcript)
    await _write_remote_text(env, f"{config.harness_dir.rstrip('/')}/report.json", json.dumps(final, ensure_ascii=False, indent=2))
    await _write_remote_text(
        env,
        f"{config.harness_dir.rstrip('/')}/management/report.json",
        json.dumps(final, ensure_ascii=False, indent=2),
    )
    await _write_remote_text(env, f"{config.harness_dir.rstrip('/')}/management/management_transcript.txt", transcript)
    _append_event(events_path, "role_harness_done", final)
    return final


@dataclass
class _GateContext:
    """Context + evolving state for the end-of-round human gate.

    Bundles the run-scoped dependencies (hook, task, rounds, paths, config) with
    the loop state the gate updates (round budget, completion, abort reason,
    carryover instructions). The gate is thus a single module-level function the
    run loop calls directly, reading results straight back from this object, with no
    thin wrapper and no return-then-reassign dance.
    """

    human_hook: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None
    task: str
    rounds: list[ManagedRound]
    log_dir: Path
    config: HarnessConfig
    events_path: Path
    round_budget: int
    completion_satisfied: bool = False
    abort_reason: str = ""
    carryover_instructions: str = ""


async def _human_gate(ctx: _GateContext, outcome: str, round_index: int, task_state: str, question: str = "", answers: list[str] | None = None) -> bool:
    """End-of-round human-in-the-loop gate; mutates ``ctx``, returns True to stop.

    ``outcome`` is this round's result (``completed`` / ``blocked`` / ``ask`` /
    ``progress``). With a hook, the dashboard decides whether to raise a gate
    (completion, max rounds, blocked, manager asking the user, or repeated
    failures) and whether to continue or stop. ``ask`` always needs a human, so
    without a hook the run stops (no channel to answer). On "continue" the gate
    reopens / extends the budget and stores any injected instructions (including
    the human's answer to an ``ask``) on ``ctx``.
    """
    reached_max = (not ctx.completion_satisfied) and round_index >= ctx.round_budget

    if ctx.human_hook is None:
        if ctx.completion_satisfied:
            return True
        if outcome == "blocked":
            ctx.abort_reason = "manager_blocked"
            return True
        if outcome == "ask":
            ctx.abort_reason = "needs_human_input"
            return True
        if reached_max:
            ctx.abort_reason = "max_rounds_exhausted"
            return True
        return False

    decision = await ctx.human_hook(
        {
            "phase": "end_of_round",
            "outcome": outcome,
            "reached_max": reached_max,
            "round_index": round_index,
            "task": ctx.task,
            "task_state": task_state,
            "question": question,
            "answers": list(answers or []),
            "rounds": [asdict(item) for item in ctx.rounds],
            "log_dir": str(ctx.log_dir),
        }
    )
    decision = decision if isinstance(decision, dict) else {}
    instructions = str(decision.get("instructions") or "").strip()
    if instructions:
        ctx.carryover_instructions = instructions
    action = str(decision.get("action") or "continue")

    if action == "stop":
        if reached_max:
            ctx.abort_reason = "max_rounds_exhausted"
        elif outcome == "blocked":
            ctx.abort_reason = "manager_blocked"
        elif outcome == "ask":
            ctx.abort_reason = "human_abort"
        elif not ctx.completion_satisfied:
            ctx.abort_reason = "human_abort"
        return True

    # continue: reopen / extend the budget when we were about to finish.
    if outcome == "completed":
        ctx.completion_satisfied = False
    if reached_max or outcome in ("completed", "blocked"):
        extra = int(decision.get("extra_rounds") or ctx.config.max_total_episodes or 1)
        ctx.round_budget = round_index + max(1, extra)
        _append_event(
            ctx.events_path,
            "human_continue_after_finish",
            {"round": round_index, "outcome": outcome, "extra_rounds": max(1, extra)},
        )
    return False


def _executor_binding(
    *,
    next_step: RoleNextStep,
    gui_executor_agent: AgentAdapter,
    cli_executor_agent: AgentAdapter,
    gui_executor_budget: EpisodeBudget,
    cli_executor_budget: EpisodeBudget,
) -> tuple[AgentAdapter, EpisodeBudget]:
    if next_step == MANAGER_NEXT_GUI:
        return gui_executor_agent, gui_executor_budget
    return cli_executor_agent, cli_executor_budget


async def _run_role_episode(
    agent: AgentAdapter,
    prompt: str,
    env: Environment,
    budget: EpisodeBudget,
    *,
    live_trajectory_path: str | None = None,
) -> EpisodeResult:
    """Normalize cooperative cancellation for every adapter implementation."""
    started = time.monotonic()
    try:
        return await agent.run_episode(
            prompt,
            env,
            budget,
            live_trajectory_path=live_trajectory_path,
        )
    except asyncio.CancelledError:
        return EpisodeResult(
            status="cancelled",
            error="Execution cancelled by operator",
            duration_ms=int((time.monotonic() - started) * 1000),
            metadata={"cancelled": True},
        )


async def _auditor_report_with_format_repair(
    *,
    env: Environment,
    config: HarnessConfig,
    round_dir: Path,
    events_path: Path,
    format_repair_agent: AgentAdapter,
    auditor_budget: EpisodeBudget,
    primary_result: EpisodeResult,
    round_index: int,
) -> tuple[str, dict[str, Any]]:
    status = _episode_status(primary_result)
    raw_report = auditor_report_text_from_episode_result(primary_result)
    if not _should_repair_auditor_format(primary_result, raw_report):
        return _auditor_report_text(
            primary_result, round_index, language=config.prompt_language
        ), status

    repair_prompt = build_role_auditor_format_repair_prompt(
        report_text=raw_report,
        language=config.prompt_language,
    )
    _write_local(round_dir / "auditor_format_repair_input.txt", repair_prompt)
    await _write_remote_round_text(env, config, round_index, "auditor_format_repair_input.txt", repair_prompt)
    repair_budget = _format_repair_budget(auditor_budget)
    _append_event(
        events_path,
        "auditor_format_repair_start",
        {
            "round": round_index,
            "prompt_chars": len(repair_prompt),
            "budget": _budget_to_dict(repair_budget),
        },
    )
    repair_result = await _run_role_episode(
        format_repair_agent,
        repair_prompt,
        env,
        repair_budget,
        live_trajectory_path=str(round_dir / "auditor_format_repair_raw_trajectory.jsonl"),
    )
    _save_role_result(round_dir, "auditor_format_repair", repair_result)
    repair_raw_report = auditor_report_text_from_episode_result(repair_result)
    repair_valid = _should_accept_auditor_format_repair(repair_result, repair_raw_report)
    status = {
        **status,
        "format_repair_attempted": True,
        "format_repair_accepted": repair_valid,
        "format_repair_status": _episode_status(repair_result),
    }
    _append_event(
        events_path,
        "auditor_format_repair_done",
        {
            "round": round_index,
            "accepted": repair_valid,
            "report_chars": len(repair_raw_report),
            "status": _episode_status(repair_result),
        },
    )
    if repair_valid:
        corrected = EpisodeResult(
            status=primary_result.status,
            actions_log=repair_raw_report,
            error=primary_result.error,
            duration_ms=primary_result.duration_ms + repair_result.duration_ms,
            metadata=primary_result.metadata,
        )
        return _auditor_report_text(
            corrected, round_index, language=config.prompt_language
        ), status
    return _auditor_report_text(
        repair_result, round_index, language=config.prompt_language
    ), status


def _should_repair_auditor_format(result: EpisodeResult, report_text: str) -> bool:
    if result.status != "done":
        return False
    if _hard_runtime_signal_labels(result):
        return False
    return not has_valid_auditor_control_header(report_text)


def _should_accept_auditor_format_repair(result: EpisodeResult, report_text: str) -> bool:
    if result.status != "done":
        return False
    if _hard_runtime_signal_labels(result):
        return False
    if _workspace_mutation_detected(result):
        return False
    return has_valid_auditor_control_header(report_text)


def _format_repair_budget(budget: EpisodeBudget) -> EpisodeBudget:
    return EpisodeBudget(
        max_duration_seconds=max(30, min(budget.max_duration_seconds, 120)),
    )


def _auditor_report_text(
    result: EpisodeResult,
    round_index: int,
    *,
    language: str = "en",
) -> str:
    report = audit_report_from_episode_result(result, round_index, language=language)
    if report.report_text.strip():
        return report.report_text.strip()
    visible = _visible_output(result).strip()
    if visible:
        return visible
    if language == "en":
        return (
            "Status: blocked\n"
            "Integrity: suspect\n"
            "Contract audit: unknown\n"
            "Audit facts: the auditor produced no readable natural-language report.\n"
            "Next step: retry the audit or schedule a smaller subtask of the same type."
        )
    return (
        "状态: blocked\n"
        "完整性: suspect\n"
        "契约审计: unknown\n"
        "审计事实: auditor 没有产生可读取的自然语言审计报告。\n"
        "下一步: 任务管理器应重试审计或生成更小的同类型子任务。"
    )


def _latest_auditor_is_clean_complete(rounds: list[ManagedRound], *, language: str = "en") -> bool:
    for item in reversed(rounds):
        if item.auditor_status.get("invalid_completion") or item.auditor_status.get("invalid_plan"):
            continue
        if not item.auditor_report.strip():
            continue
        report = parse_audit_report(item.auditor_report, item.round_index, language=language)
        return (
            report.status == "complete"
            and report.integrity_status == "clean"
            and report.contract_audit_status == "aligned"
        )
    return False


def _final_report(
    *,
    task: str,
    rounds: list[ManagedRound],
    completion_satisfied: bool,
    abort_reason: str,
    last_plan: str,
    task_state: str,
    task_contract: str,
    max_rounds: int,
    elapsed_seconds: float,
) -> dict[str, Any]:
    # Final status is a harness-level decision, not the last executor agent's self
    # claim. The auditor artifact remains the natural-language audit report.
    latest_report_text = _latest_auditor_report_text(rounds)
    status = (
        "complete"
        if completion_satisfied
        else "cancelled"
        if abort_reason == "user_cancelled"
        else "blocked"
        if abort_reason == "manager_blocked"
        else "incomplete"
    )
    return {
        "schema_version": 2,
        "variant": ROLE_VARIANT,
        "mode": "role_management",
        "status": status,
        "task": task,
        "completion_satisfied": completion_satisfied,
        "completion_authority": "manager_with_role_auditors",
        "rounds_run": len(rounds),
        "max_rounds": max_rounds,
        "abort_reason": abort_reason,
        "last_plan": last_plan,
        "current_task_state": task_state,
        "current_task_contract": task_contract,
        "latest_auditor_report": latest_report_text,
        "rounds": [asdict(item) for item in rounds],
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


def _latest_auditor_report_text(rounds: list[ManagedRound]) -> str:
    # Round state intentionally stores auditor reports as natural language. The
    # parser is only a transient stop-condition check.
    for item in reversed(rounds):
        if item.auditor_status.get("invalid_completion") or item.auditor_status.get("invalid_plan"):
            continue
        if item.auditor_report.strip():
            return item.auditor_report.strip()
    return ""


def _visible_output(result: EpisodeResult) -> str:
    # Adapters can expose a clean assistant-visible output in metadata. Falling
    # back to actions_log keeps simple command adapters usable.
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    for key in VISIBLE_OUTPUT_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if metadata.get("actions_log_diagnostics_only"):
        return ""
    raw = result.actions_log or ""
    # Decode the final assistant-visible text from Claude or Codex JSONL while
    # keeping the complete machine trajectory in actions_log for diagnostics.
    decoded = decode_agent_visible_output(raw)
    return decoded if decoded else raw


def _episode_status(result: EpisodeResult) -> dict[str, Any]:
    # Keep status compact in round records; full raw output is stored separately.
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    return {
        "status": result.status,
        "error": result.error,
        "duration_ms": result.duration_ms,
        "agent_done": metadata.get("agent_done"),
        "exit_code": metadata.get("exit_code"),
        "runtime_signals": metadata.get("runtime_signals"),
    }


def _hard_runtime_signal_labels(result: EpisodeResult) -> list[str]:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    return hard_signal_labels(metadata.get("runtime_signals"))


def _workspace_mutation_detected(result: EpisodeResult) -> bool:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    return bool(metadata.get("verifier_workspace_mutation_detected"))


def _save_role_result(round_dir: Path, role_name: str, result: EpisodeResult) -> None:
    # Raw trajectories are stored locally for audit/debugging, while prompt
    # construction only consumes visible output and auditor reports. Claude Code
    # emits one JSON object per line (stream-json), so the trajectory is saved as
    # .jsonl to reflect its real format and make downstream parsing explicit.
    trajectory_path = round_dir / f"{role_name}_raw_trajectory.jsonl"
    preserved_live_trajectory = False
    live_trajectory = ""
    if trajectory_path.exists():
        live_trajectory = trajectory_path.read_text(encoding="utf-8", errors="replace")
    final_trajectory = result.actions_log or ""
    if live_trajectory and (
        not final_trajectory
        or (live_trajectory.startswith(final_trajectory) and len(live_trajectory) > len(final_trajectory))
    ):
        # Timeout/cancellation used to return empty stdout and erase the JSONL
        # that the live tee had already flushed. It also remains authoritative
        # if an interrupted final read captured only a shorter prefix.
        preserved_live_trajectory = True
    else:
        _write_local(trajectory_path, final_trajectory)
    metadata = {
        "status": result.status,
        "error": result.error,
        "duration_ms": result.duration_ms,
        "metadata": result.metadata,
        "live_trajectory_preserved": preserved_live_trajectory,
    }
    _write_local(round_dir / f"{role_name}_metadata.json", json.dumps(_json_safe(metadata), ensure_ascii=False, indent=2))


async def _record_round(
    env: Environment,
    config: HarnessConfig,
    role_dir: Path,
    events_path: Path,
    record: ManagedRound,
) -> None:
    # rounds.jsonl is the append-only local ledger; round.json mirrors the same
    # state into the task VM for later inspection.
    payload = json.dumps(asdict(record), ensure_ascii=False, indent=2)
    rounds_jsonl = role_dir / "rounds.jsonl"
    with rounds_jsonl.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n")
    await _write_remote_round_text(env, config, record.round_index, "round.json", payload)
    _append_event(events_path, "managed_round_recorded", asdict(record))


async def _ensure_remote_layout(env: Environment, config: HarnessConfig) -> None:
    # The remote layout is intentionally small: final report plus per-round role
    # artifacts under `.harness/management`.
    harness_dir = config.harness_dir.rstrip("/")
    for path in (
        harness_dir,
        f"{harness_dir}/management",
        f"{harness_dir}/management/rounds",
    ):
        try:
            await ensure_remote_dir(env, path)
        except Exception as exc:
            logger.warning("remote trace directory setup skipped for %s: %s", path, exc)


async def _write_remote_round_text(
    env: Environment,
    config: HarnessConfig,
    round_index: int,
    name: str,
    text: str,
) -> None:
    remote_dir = f"{config.harness_dir.rstrip('/')}/management/rounds/round_{round_index:03d}"
    try:
        await ensure_remote_dir(env, remote_dir)
        await write_remote_text(env, f"{remote_dir}/{name}", text)
    except Exception as exc:
        logger.warning(
            "remote trace write skipped for round_%03d/%s: %s",
            round_index,
            name,
            exc,
        )


async def _write_remote_text(env: Environment, path: str, text: str) -> None:
    try:
        await write_remote_text(env, path, text)
    except Exception as exc:
        logger.warning("remote trace write skipped for %s: %s", path, exc)


def _write_local(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _budget_to_dict(budget: EpisodeBudget) -> dict[str, int]:
    return {
        "max_duration_seconds": budget.max_duration_seconds,
    }


def _append_event(path: Path, event: str, payload: dict[str, Any]) -> None:
    record = {"ts": time.time(), "event": event, **_json_safe(payload)}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
