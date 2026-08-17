from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import stat as stat_module
import time
from dataclasses import asdict, dataclass, field
import traceback
from pathlib import Path
from typing import Any, Awaitable, Callable

from .adapters.base import AgentAdapter
from .agent_logs import (
    assistant_texts as decode_agent_assistant_texts,
    visible_output as decode_agent_visible_output,
)
from .environment.base import Environment
from .environment.remote_files import ensure_remote_dir, write_remote_text
from .runtime_signals import hard_signal_labels
from .provider_errors import classify_agent_runtime_failure
from .trajectory_artifacts import persist_trajectory_artifacts
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
    build_role_final_response_prompt,
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
from .supervisor.control_bus import (
    _append_jsonl as _append_jsonl_nofollow,
    _atomic_bytes_write,
    _ensure_dir_nofollow,
    _open_nofollow,
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
_MAX_SAVED_TRAJECTORY_BYTES = 16 * 1024 * 1024
_MAX_FAILURE_REPORT_BYTES = 1 * 1024 * 1024
_MAX_FAILURE_EVENTS_BYTES = 4 * 1024 * 1024
_MAX_FAILURE_EVENT_RECORDS = 50_000


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


@dataclass
class _RunProgress:
    """Mutable state retained by the worker boundary if the loop crashes."""

    rounds: list[ManagedRound] = field(default_factory=list)
    gate: _GateContext | None = None
    started_at: float | None = None


async def run(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run the management loop and always leave a durable terminal record.

    The historical implementation only wrote ``report.json`` on the happy
    path.  An adapter, environment, or filesystem exception therefore left a
    worker with no terminal event; the supervisor had no evidence and could
    incorrectly describe it as completed.  Keep the execution kernel in
    ``_run_impl`` and make this boundary the single crash/cancellation guard.
    """

    task = str(kwargs.get("task") or "")
    config = kwargs.get("config")
    run_progress = _RunProgress()
    try:
        return await _run_impl(*args, _run_progress=run_progress, **kwargs)
    except asyncio.CancelledError as exc:
        return _write_terminal_failure(
            config,
            task,
            status="cancelled",
            reason="worker task was cancelled",
            exc=exc,
            abort_reason="worker_cancelled",
            progress=run_progress,
        )
    except BaseException as exc:  # worker boundary: persist even non-Exception failures
        # KeyboardInterrupt/SystemExit are intentionally converted to a
        # failed/cancelled artifact here; the CLI process still exits through
        # its normal return path after the report is durable.
        return _write_terminal_failure(
            config,
            task,
            status="failed",
            reason=f"management loop crashed: {exc}",
            exc=exc,
            abort_reason="worker_exception",
            progress=run_progress,
        )


async def _run_impl(
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
    final_response_agent: AgentAdapter | None = None,
    human_hook: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
    _run_progress: _RunProgress | None = None,
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

    ``progress`` is an optional synchronous ``(event, payload)`` sink for
    operator-facing status lines (the CLI prints them to the console).
    """

    def emit(event: str, **payload: Any) -> None:
        if progress is None:
            return
        try:
            progress(event, payload)
        except Exception:  # progress reporting must never break a run
            logger.debug("progress callback failed for %s", event, exc_info=True)

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

    # Keep every local ledger path canonical before deriving event ids or
    # opening anchored descriptors.  A relative ``--log-dir`` is valid for
    # the standalone CLI, but ``Path.parents`` on a relative path would make
    # the event id depend on the current working directory (and could collide
    # across runs).
    log_dir = Path(config.log_dir).expanduser().resolve(strict=False)
    role_dir = log_dir / "role_orchestration"
    rounds_dir = role_dir / "rounds"
    _ensure_dir_nofollow(rounds_dir)
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
        env=env,
        role_dir=role_dir,
        response_agent=final_response_agent or manager_agent,
        emit=emit,
    )
    if _run_progress is not None:
        # The list and gate are mutated in place, so the outer crash boundary
        # always sees the latest completed rounds without checkpoint rewrites.
        _run_progress.rounds = rounds
        _run_progress.gate = gate
        _run_progress.started_at = started

    while round_index < gate.round_budget:
        round_index += 1
        round_dir = rounds_dir / f"round_{round_index:03d}"
        _ensure_dir_nofollow(round_dir)
        emit("round_start", round=round_index, round_budget=gate.round_budget)

        # The manager sees the original task, its maintained task state,
        # and auditor reports. It never receives raw trajectories or previous
        # full prompts.
        manager_prompt = build_role_manager_prompt(
            task=task,
            rounds=rounds,
            round_index=round_index,
            task_state=current_task_state,
            task_contract=current_task_contract,
            round_budget=gate.round_budget,
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
        emit("role_start", round=round_index, role="manager")

        manager_result = await _run_role_episode(
            manager_agent,
            manager_prompt,
            env,
            manager_budget,
            live_trajectory_path=str(round_dir / "manager_raw_trajectory.jsonl"),
        )
        _save_role_result(
            round_dir,
            "manager",
            manager_result,
            episode_root=log_dir / "manager_episodes",
        )
        if manager_result.status == "cancelled":
            gate.abort_reason = "user_cancelled"
            _append_event(
                events_path,
                "role_harness_cancelled",
                {
                    "round": round_index,
                    "phase": "manager",
                    **_episode_event_fields(manager_result, event_status="cancelled"),
                },
            )
            break
        manager_failure = classify_agent_runtime_failure(manager_result)
        if manager_failure is not None:
            recoverable_timeout = manager_failure.kind == "timeout"
            plan_text = (
                ("Next: invalid\n\nReason:\n" if recoverable_timeout else "Next: blocked\n\nReason:\n")
                + manager_failure.user_message
                if config.prompt_language == "en"
                else ("下一步: 无效\n\n原因:\n" if recoverable_timeout else "下一步: 阻塞\n\n阻塞原因:\n")
                + manager_failure.user_message
            )
            record = ManagedRound(
                round_index=round_index,
                next_step=MANAGER_NEXT_INVALID if recoverable_timeout else MANAGER_NEXT_BLOCKED,
                plan_text=plan_text,
                harness_feedback=manager_failure.user_message,
                task_state=current_task_state,
                task_contract=current_task_contract,
                manager_status=_failed_episode_status(
                    manager_result, manager_failure.user_message
                ),
                auditor_status={"invalid_plan": True} if recoverable_timeout else {},
            )
            _write_local(round_dir / "manager_plan.txt", plan_text)
            _write_local(round_dir / "harness_feedback.txt", manager_failure.user_message)
            rounds.append(record)
            await _record_round(env, config, role_dir, events_path, record)
            _append_event(
                events_path,
                "agent_runtime_failed",
                {
                    "round": round_index,
                    "phase": "manager",
                    "kind": manager_failure.kind,
                    "message": manager_failure.message,
                    **_episode_event_fields(
                        manager_result,
                        event_status="failed",
                        error_message=manager_failure.user_message,
                    ),
                },
            )
            emit(
                "role_done",
                round=round_index,
                role="manager",
                status="failed",
                duration_ms=manager_result.duration_ms,
                error=manager_failure.user_message,
            )
            if recoverable_timeout:
                if await _human_gate(gate, "progress", round_index, current_task_state):
                    break
                continue
            gate.abort_reason = manager_failure.abort_reason
            gate.failure_reason = manager_failure.user_message
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
                **_episode_event_fields(manager_result, event_status="completed"),
            },
        )
        emit(
            "role_done",
            round=round_index,
            role="manager",
            status=manager_result.status,
            duration_ms=manager_result.duration_ms,
            next_step=next_step,
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
            workspace_path=config.workspace_path,
            language=config.prompt_language,
        )
        _write_local(round_dir / "executor_prompt.txt", executor_prompt)
        await _write_remote_round_text(env, config, round_index, "executor_prompt.txt", executor_prompt)
        _append_event(
            events_path,
            "executor_role_start",
            {"round": round_index, "role": next_step, "prompt_chars": len(executor_prompt), "budget": _budget_to_dict(executor_budget)},
        )
        emit("role_start", round=round_index, role=f"{next_step}_executor")

        executor_result = await _run_role_episode(
            executor_agent,
            executor_prompt,
            env,
            executor_budget,
            live_trajectory_path=str(round_dir / "executor_raw_trajectory.jsonl"),
        )
        executor_episode_root = log_dir / (
            "gui_executor_episodes" if next_step == MANAGER_NEXT_GUI else "cli_executor_episodes"
        )
        executor_final_screenshot = (
            await _capture_environment_screenshot(env)
            if next_step == MANAGER_NEXT_GUI
            else None
        )
        _save_role_result(
            round_dir,
            "executor",
            executor_result,
            episode_root=executor_episode_root,
            final_screenshot=executor_final_screenshot,
        )
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
                {
                    "round": round_index,
                    "phase": "executor",
                    **_episode_event_fields(executor_result, event_status="cancelled"),
                },
            )
            break
        executor_failure = classify_agent_runtime_failure(executor_result)
        if executor_failure is not None:
            recoverable_timeout = executor_failure.kind == "timeout"
            record = ManagedRound(
                round_index=round_index,
                next_step=next_step,
                plan_text=plan_text,
                executor_output=executor_output if recoverable_timeout else "",
                harness_feedback=executor_failure.user_message,
                task_state=current_task_state,
                task_contract=current_task_contract,
                related_report_refs=related_report_refs,
                manager_status=_episode_status(manager_result),
                executor_status=_failed_episode_status(
                    executor_result, executor_failure.user_message
                ),
            )
            _write_local(round_dir / "harness_feedback.txt", executor_failure.user_message)
            rounds.append(record)
            await _record_round(env, config, role_dir, events_path, record)
            _append_event(
                events_path,
                "agent_runtime_failed",
                {
                    "round": round_index,
                    "phase": "executor",
                    "kind": executor_failure.kind,
                    "message": executor_failure.message,
                    **_episode_event_fields(
                        executor_result,
                        event_status="failed",
                        error_message=executor_failure.user_message,
                    ),
                },
            )
            emit(
                "role_done",
                round=round_index,
                role=f"{next_step}_executor",
                status="failed",
                duration_ms=executor_result.duration_ms,
                error=executor_failure.user_message,
            )
            if recoverable_timeout:
                if await _human_gate(gate, "progress", round_index, current_task_state):
                    break
                continue
            gate.abort_reason = executor_failure.abort_reason
            gate.failure_reason = executor_failure.user_message
            break
        await _write_remote_round_text(env, config, round_index, "executor_output.txt", executor_output)
        _append_event(
            events_path,
            "executor_role_done",
            {
                "round": round_index,
                "role": next_step,
                "output_chars": len(executor_output),
                **_episode_event_fields(executor_result, event_status="completed"),
            },
        )
        emit(
            "role_done",
            round=round_index,
            role=f"{next_step}_executor",
            status=executor_result.status,
            duration_ms=executor_result.duration_ms,
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
            workspace_path=config.workspace_path,
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
        emit("role_start", round=round_index, role=f"{next_step}_auditor")

        auditor_result = await _run_role_episode(
            auditor_for_step,
            auditor_prompt,
            env,
            auditor_budget,
            live_trajectory_path=str(round_dir / "auditor_raw_trajectory.jsonl"),
        )
        auditor_episode_root = log_dir / (
            "gui_auditor_episodes" if next_step == MANAGER_NEXT_GUI else "cli_auditor_episodes"
        )
        auditor_final_screenshot = (
            await _capture_environment_screenshot(env)
            if next_step == MANAGER_NEXT_GUI
            else None
        )
        _save_role_result(
            round_dir,
            "auditor",
            auditor_result,
            episode_root=auditor_episode_root,
            final_screenshot=auditor_final_screenshot,
        )
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
                {
                    "round": round_index,
                    "phase": "auditor",
                    **_episode_event_fields(auditor_result, event_status="cancelled"),
                },
            )
            break
        auditor_failure = classify_agent_runtime_failure(auditor_result)
        if auditor_failure is not None:
            recoverable_timeout = auditor_failure.kind == "timeout"
            record = ManagedRound(
                round_index=round_index,
                next_step=next_step,
                plan_text=plan_text,
                executor_output=executor_output,
                harness_feedback=auditor_failure.user_message,
                task_state=current_task_state,
                task_contract=current_task_contract,
                related_report_refs=related_report_refs,
                manager_status=_episode_status(manager_result),
                executor_status=_episode_status(executor_result),
                auditor_status=_failed_episode_status(
                    auditor_result, auditor_failure.user_message
                ),
            )
            _write_local(round_dir / "harness_feedback.txt", auditor_failure.user_message)
            rounds.append(record)
            await _record_round(env, config, role_dir, events_path, record)
            _append_event(
                events_path,
                "agent_runtime_failed",
                {
                    "round": round_index,
                    "phase": "auditor",
                    "kind": auditor_failure.kind,
                    "message": auditor_failure.message,
                    **_episode_event_fields(
                        auditor_result,
                        event_status="failed",
                        error_message=auditor_failure.user_message,
                    ),
                },
            )
            emit(
                "role_done",
                round=round_index,
                role=f"{next_step}_auditor",
                status="failed",
                duration_ms=auditor_result.duration_ms,
                error=auditor_failure.user_message,
            )
            if recoverable_timeout:
                if await _human_gate(gate, "progress", round_index, current_task_state):
                    break
                continue
            gate.abort_reason = auditor_failure.abort_reason
            gate.failure_reason = auditor_failure.user_message
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
            episode_root=auditor_episode_root,
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
                {
                    "round": round_index,
                    "phase": "auditor_format_repair",
                    "status": "cancelled",
                    "episode_status": repair_status,
                },
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
                **_episode_event_fields(auditor_result, event_status="completed"),
            },
        )
        audit = parse_audit_report(auditor_report, round_index, language=config.prompt_language)
        emit(
            "role_done",
            round=round_index,
            role=f"{next_step}_auditor",
            status=auditor_result.status,
            duration_ms=auditor_result.duration_ms,
            audit_status=audit.status,
            integrity_status=audit.integrity_status,
            contract_audit_status=audit.contract_audit_status,
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
        final_response=gate.final_response,
        failure_reason=gate.failure_reason,
    )
    _write_local(role_dir / "report.json", json.dumps(final, ensure_ascii=False, indent=2) + "\n")
    _write_local(log_dir / "report.json", json.dumps(final, ensure_ascii=False, indent=2) + "\n")
    transcript = format_management_history(rounds, include_empty=True, max_chars=200_000)
    _write_local(role_dir / "orchestration_transcript.txt", transcript)
    _merge_episode_logs(log_dir)
    await _write_remote_text(env, f"{config.harness_dir.rstrip('/')}/report.json", json.dumps(final, ensure_ascii=False, indent=2))
    await _write_remote_text(
        env,
        f"{config.harness_dir.rstrip('/')}/orchestration/report.json",
        json.dumps(final, ensure_ascii=False, indent=2),
    )
    await _write_remote_text(env, f"{config.harness_dir.rstrip('/')}/orchestration/orchestration_transcript.txt", transcript)
    _append_event(events_path, "role_harness_done", final)
    emit(
        "run_done",
        status=final["status"],
        completion_satisfied=final["completion_satisfied"],
        abort_reason=final["abort_reason"],
        rounds_run=final["rounds_run"],
        elapsed_seconds=final["elapsed_seconds"],
        report_path=str(log_dir / "report.json"),
    )
    return final


def _discard_progress(*_args: Any, **_kwargs: Any) -> None:
    """Default ``emit`` for gate contexts built without a progress sink."""


def _mark_cancelled(ctx: _GateContext) -> None:
    """Record an operator cancellation raised while the reply was being written.

    Completion is cleared too: `_final_report` ranks it above the abort reason, so
    a cancelled run would otherwise still be reported as complete.
    """
    ctx.abort_reason = "user_cancelled"
    ctx.completion_satisfied = False


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
    # The gate writes the user-facing reply before asking the operator to decide,
    # so it needs the pieces an episode requires.
    env: Environment | None = None
    role_dir: Path | None = None
    response_agent: AgentAdapter | None = None
    emit: Callable[..., None] = field(default=_discard_progress)
    completion_satisfied: bool = False
    abort_reason: str = ""
    carryover_instructions: str = ""
    # Dashboard follow-ups are authoritative user input.  ``carryover`` is
    # consumed by the next Manager round, while this history remains available
    # to the final-response role so reply-specific requirements are not lost.
    operator_instructions: list[str] = field(default_factory=list)
    # Round that already attempted a reply, so one gate cannot write a round's
    # reply artifacts twice (which would corrupt the saved trajectory metadata).
    response_round: int = 0
    final_response: str = ""
    # A terminal agent/provider failure is not a task-planning decision.  Keep
    # its actionable cause separate from ``abort_reason`` so Supervisor/Web can
    # show the provider's real message instead of a generic round-limit gate.
    failure_reason: str = ""


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
    # `ctx.abort_reason` is only assigned below, so the reason is derived here to
    # keep "ran out of rounds" distinguishable from "blocked" in the reply. `ask`
    # is excluded: the manager is asking a mid-task question, so the run is not
    # ending and a reply written here would be discarded on the answer.
    ending = (
        ""
        if ctx.completion_satisfied
        else "manager_blocked"
        if outcome == "blocked"
        else "max_rounds_exhausted"
        if reached_max
        else None
    )

    # Written before the operator is asked anything, so the decision to accept the
    # result or push the run further is made against the actual answer.
    if ending is not None and await _write_final_response(ctx, round_index, task_state, ending):
        _mark_cancelled(ctx)
        return True

    if ctx.human_hook is None:
        if ctx.completion_satisfied:
            return True
        if outcome == "blocked":
            ctx.abort_reason = "manager_blocked"
            return True
        if outcome == "ask":
            # Nothing can answer the question, so this ending is only known here.
            ctx.abort_reason = "needs_human_input"
            if await _write_final_response(ctx, round_index, task_state, ctx.abort_reason):
                _mark_cancelled(ctx)
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
            "final_response": ctx.final_response,
            "rounds": [asdict(item) for item in ctx.rounds],
            "log_dir": str(ctx.log_dir),
        }
    )
    decision = decision if isinstance(decision, dict) else {}
    instructions = str(decision.get("instructions") or "").strip()
    if instructions:
        ctx.carryover_instructions = instructions
        ctx.operator_instructions.append(instructions)
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
        # The operator can stop on a round the harness did not treat as an ending
        # (an `ask` or repeated-failure gate), which leaves no reply written yet.
        if await _write_final_response(ctx, round_index, task_state, ctx.abort_reason):
            _mark_cancelled(ctx)
        return True

    # continue: reopen / extend the budget when we were about to finish. The reply
    # just shown describes a run that is no longer over, so it is discarded and
    # rewritten at whatever ending comes next.
    if ctx.final_response:
        ctx.final_response = ""
        await _discard_final_response(ctx)
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
    episode_root: Path,
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
    _save_role_result(
        round_dir,
        "auditor_format_repair",
        repair_result,
        episode_root=episode_root,
    )
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
            **_episode_event_fields(repair_result, event_status="completed"),
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


async def _write_final_response(
    ctx: _GateContext, round_index: int, task_state: str, ending: str
) -> bool:
    """Answer the original request in the user's terms, storing it on ``ctx``.

    Every other role writes for the next role, so without this the operator only
    sees audit protocol text. Failure must never cost the run its report, so any
    problem degrades to an empty reply. Returns True when the operator cancelled
    during generation, which the caller turns into a run-level abort.
    """
    if ctx.final_response or not ctx.rounds:
        return False
    if ctx.env is None or ctx.role_dir is None or ctx.response_agent is None:
        return False
    if ending == "user_cancelled" or ctx.response_round == round_index:
        return False
    ctx.response_round = round_index

    status = (
        "complete"
        if ctx.completion_satisfied
        else "blocked"
        if ending == "manager_blocked"
        else "incomplete"
    )
    prompt = build_role_final_response_prompt(
        task=ctx.task,
        rounds=ctx.rounds,
        status=status,
        abort_reason=ending,
        task_state=task_state,
        operator_instructions="\n\n".join(ctx.operator_instructions),
        language=ctx.config.prompt_language,
    )
    budget = _final_response_budget(ctx.config.manager_budget)
    # Stored per round, like the other roles, so a discarded reply keeps its own
    # artifacts and the dashboard's round trajectory viewer can reach them.
    round_dir = ctx.role_dir / "rounds" / f"round_{round_index:03d}"
    round_dir.mkdir(parents=True, exist_ok=True)
    _write_local(round_dir / "final_response_input.txt", prompt)
    _append_event(
        ctx.events_path,
        "final_response_start",
        {"round": round_index, "prompt_chars": len(prompt), "budget": _budget_to_dict(budget)},
    )
    ctx.emit("role_start", round=round_index, role="final_response")

    try:
        result = await _run_role_episode(
            ctx.response_agent,
            prompt,
            ctx.env,
            budget,
            live_trajectory_path=str(round_dir / "final_response_raw_trajectory.jsonl"),
        )
    except Exception:
        logger.warning("final response episode failed", exc_info=True)
        _append_event(
            ctx.events_path,
            "final_response_done",
            {
                "round": round_index,
                "accepted": False,
                "error": "episode_failed",
                "status": "failed",
            },
        )
        ctx.emit("role_done", round=round_index, role="final_response", status="error")
        return False

    _save_role_result(
        round_dir,
        "final_response",
        result,
        episode_root=ctx.log_dir / "final_response_episodes",
    )
    response = _visible_output(result).strip() if result.status == "done" else ""
    if response:
        ctx.final_response = response
        _write_local(round_dir / "final_response.txt", response)
        _write_local(ctx.role_dir / "final_response.txt", response)
        await _write_remote_text(
            ctx.env,
            f"{ctx.config.harness_dir.rstrip('/')}/orchestration/final_response.txt",
            response,
        )
    _append_event(
        ctx.events_path,
        "final_response_done",
        {
            "round": round_index,
            "accepted": bool(response),
            "response_chars": len(response),
            **_episode_event_fields(result, event_status="completed"),
        },
    )
    ctx.emit(
        "role_done",
        round=round_index,
        role="final_response",
        status=result.status,
        duration_ms=result.duration_ms,
    )
    # Cancellation is normalized into a result by `_run_role_episode`, so without
    # this the operator's Ctrl+C would be silently absorbed here.
    return result.status == "cancelled"


async def _discard_final_response(ctx: _GateContext) -> None:
    """Drop the published reply once the operator reopens the run.

    The round's own artifacts stay for auditing; only the published copies go, so
    a later ending that yields no reply cannot leave stale text next to a report
    whose ``final_response`` is empty.
    """
    if ctx.role_dir is not None:
        with contextlib.suppress(OSError):
            (ctx.role_dir / "final_response.txt").unlink(missing_ok=True)
    if ctx.env is not None:
        await _write_remote_text(
            ctx.env, f"{ctx.config.harness_dir.rstrip('/')}/orchestration/final_response.txt", ""
        )
    _append_event(ctx.events_path, "final_response_discarded", {"round": ctx.response_round})


def _final_response_budget(budget: EpisodeBudget) -> EpisodeBudget:
    return EpisodeBudget(
        max_duration_seconds=max(60, min(budget.max_duration_seconds, 180)),
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
    visible, _ = _bounded_text_tail(visible, _MAX_SAVED_TRAJECTORY_BYTES)
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
    final_response: str = "",
    failure_reason: str = "",
) -> dict[str, Any]:
    # Final status is a harness-level decision, not the last executor agent's self
    # claim. The auditor artifact remains the natural-language audit report.
    latest_report_text = _latest_auditor_report_text(rounds)
    status = (
        "complete"
        if completion_satisfied
        else "cancelled"
        if abort_reason == "user_cancelled"
        else "failed"
        if abort_reason.startswith("provider_")
        else "blocked"
        if abort_reason == "manager_blocked"
        else "incomplete"
    )
    return {
        "schema_version": 2,
        "variant": ROLE_VARIANT,
        "mode": "role_orchestration",
        "status": status,
        "task": task,
        "completion_satisfied": completion_satisfied,
        "completion_authority": "manager_with_role_auditors",
        "rounds_run": len(rounds),
        "max_rounds": max_rounds,
        "abort_reason": abort_reason,
        "failure_reason": failure_reason,
        "error": failure_reason or None,
        "last_plan": last_plan,
        "current_task_state": task_state,
        "current_task_contract": task_contract,
        "latest_auditor_report": latest_report_text,
        "final_response": final_response,
        "rounds": [asdict(item) for item in rounds],
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


def _read_local_bounded(path: Path, max_bytes: int, *, tail: bool = False) -> str | None:
    """Read a worker-owned diagnostic file through a bounded no-follow fd."""

    fd: int | None = None
    try:
        # ``O_NOFOLLOW`` on only the final component is insufficient because
        # the worker can also replace ``lh_harness``/``role_orchestration`` with a
        # symlink.  Walk every component from an anchored root descriptor.
        fd = _open_nofollow(path)
        metadata = os.fstat(fd)
        if not stat_module.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return None
        size = int(metadata.st_size)
        start = max(0, size - max_bytes) if tail else 0
        if start:
            os.lseek(fd, start, os.SEEK_SET)
        data = bytearray()
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            data.extend(chunk)
            remaining -= len(chunk)
        raw = bytes(data[:max_bytes])
        if tail and start:
            # Do not feed a partial JSONL record to the crash detector.
            first_newline = raw.find(b"\n")
            raw = raw[first_newline + 1 :] if first_newline >= 0 else b""
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _write_terminal_failure(
    config: HarnessConfig | None,
    task: str,
    *,
    status: str,
    reason: str,
    exc: BaseException,
    abort_reason: str,
    progress: _RunProgress | None = None,
) -> dict[str, Any]:
    """Best-effort local crash report and terminal event for the worker.

    This function is deliberately synchronous: it is called while the event
    loop is already unwinding, and a small local write is more reliable than
    scheduling another coroutine that may never run.  The report is bounded so
    an enormous traceback cannot become a second DoS vector.
    """

    log_dir = Path(getattr(config, "log_dir", "./lh_harness"))
    role_dir = log_dir / "role_orchestration"
    report_path = log_dir / "report.json"
    role_report_path = role_dir / "report.json"
    existing: dict[str, Any] = {}
    existing_text = _read_local_bounded(report_path, _MAX_FAILURE_REPORT_BYTES)
    if existing_text is not None:
        try:
            parsed_existing = json.loads(existing_text)
            if isinstance(parsed_existing, dict):
                existing = parsed_existing
        except json.JSONDecodeError:
            pass
    if isinstance(existing, dict) and existing.get("status") in {"complete", "completed", "cancelled", "failed", "blocked", "incomplete"}:
        # A failure during a post-report remote sync must not erase a valid
        # local authority.  The supervisor can still use the existing report.
        return existing

    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    report: dict[str, Any] = {
        "schema_version": 2,
        "variant": ROLE_VARIANT,
        "mode": "role_orchestration",
        "status": status,
        "task": task,
        "completion_satisfied": False,
        "completion_authority": "manager_with_role_auditors",
        "rounds_run": 0,
        "max_rounds": int(getattr(config, "max_total_episodes", 0) or 0),
        "abort_reason": abort_reason,
        "failure_reason": reason,
        "error": reason,
        "last_plan": "",
        "current_task_state": "",
        "current_task_contract": "",
        "latest_auditor_report": "",
        "final_response": "",
        "rounds": [],
        "elapsed_seconds": 0.0,
        "exception_type": type(exc).__name__,
        "traceback_tail": trace[-12000:],
        "supervisor_generated": False,
    }
    if progress is not None:
        rounds = list(progress.rounds)
        latest = rounds[-1] if rounds else None
        gate = progress.gate
        report.update(
            {
                "completion_satisfied": bool(gate and gate.completion_satisfied),
                "rounds_run": len(rounds),
                "max_rounds": (
                    gate.round_budget
                    if gate is not None
                    else int(getattr(config, "max_total_episodes", 0) or 0)
                ),
                "last_plan": latest.plan_text if latest is not None else "",
                "current_task_state": latest.task_state if latest is not None else "",
                "current_task_contract": latest.task_contract if latest is not None else "",
                "latest_auditor_report": _latest_auditor_report_text(rounds),
                "final_response": gate.final_response if gate is not None else "",
                "rounds": [asdict(item) for item in rounds],
                "elapsed_seconds": (
                    round(max(0.0, time.monotonic() - progress.started_at), 3)
                    if progress.started_at is not None
                    else 0.0
                ),
            }
        )
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    for target in (report_path, role_report_path):
        try:
            _atomic_bytes_write(target, encoded.encode("utf-8"))
        except OSError:
            pass
    try:
        events_path = role_dir / "events.jsonl"
        if not any(item.get("event") == "role_harness_failed" for item in _read_jsonl_local(events_path)):
            _append_event(
                events_path,
                "role_harness_cancelled" if status == "cancelled" else "role_harness_failed",
                {
                    "status": status,
                    "reason": reason,
                    "exception_type": type(exc).__name__,
                    "traceback_tail": trace[-4000:],
                },
            )
    except OSError:
        pass
    return report


def _read_jsonl_local(path: Path) -> list[dict[str, Any]]:
    raw = _read_local_bounded(path, _MAX_FAILURE_EVENTS_BYTES, tail=True)
    if raw is None:
        return []
    result: list[dict[str, Any]] = []
    for index, line in enumerate(raw.splitlines()):
        if index >= _MAX_FAILURE_EVENT_RECORDS:
            break
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


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


def _failed_episode_status(result: EpisodeResult, user_message: str) -> dict[str, Any]:
    status = _episode_status(result)
    status["status"] = "timeout" if result.status == "timeout" else "error"
    status["error"] = user_message
    return status


def _episode_event_fields(
    result: EpisodeResult,
    *,
    event_status: str,
    error_message: str | None = None,
) -> dict[str, Any]:
    """Separate public event lifecycle state from episode diagnostics."""

    episode_status = (
        _failed_episode_status(result, error_message)
        if error_message is not None
        else _episode_status(result)
    )
    return {"status": event_status, "episode_status": episode_status}


def _hard_runtime_signal_labels(result: EpisodeResult) -> list[str]:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    return hard_signal_labels(metadata.get("runtime_signals"))


def _workspace_mutation_detected(result: EpisodeResult) -> bool:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    return bool(metadata.get("verifier_workspace_mutation_detected"))


def _save_role_result(
    round_dir: Path,
    role_name: str,
    result: EpisodeResult,
    *,
    episode_root: Path | None = None,
    final_screenshot: bytes | None = None,
) -> dict[str, Any]:
    # Raw trajectories are stored locally for audit/debugging, while prompt
    # construction only consumes visible output and auditor reports. Claude Code
    # emits one JSON object per line (stream-json), so the trajectory is saved as
    # .jsonl to reflect its real format and make downstream parsing explicit.
    trajectory_path = round_dir / f"{role_name}_raw_trajectory.jsonl"
    preserved_live_trajectory = False
    live_trajectory = ""
    live_trajectory_truncated = False
    if trajectory_path.exists():
        live_trajectory, live_trajectory_truncated = _read_local_text_tail(
            trajectory_path,
            _MAX_SAVED_TRAJECTORY_BYTES,
        )
    final_trajectory, final_trajectory_truncated = _bounded_text_tail(
        result.actions_log or "",
        _MAX_SAVED_TRAJECTORY_BYTES,
    )
    if live_trajectory and (
        not final_trajectory
        or (
            not live_trajectory_truncated
            and live_trajectory.startswith(final_trajectory)
            and len(live_trajectory) > len(final_trajectory)
        )
    ):
        # Timeout/cancellation used to return empty stdout and erase the JSONL
        # that the live tee had already flushed. It also remains authoritative
        # if an interrupted final read captured only a shorter prefix.
        preserved_live_trajectory = True
    if not preserved_live_trajectory or live_trajectory_truncated:
        if preserved_live_trajectory:
            final_trajectory = live_trajectory
        _write_local(trajectory_path, final_trajectory)
    artifact_source = live_trajectory if preserved_live_trajectory else final_trajectory
    try:
        trajectory_artifacts = persist_trajectory_artifacts(
            artifact_source,
            round_dir=round_dir,
            role_name=role_name,
        )
    except (OSError, ValueError) as exc:
        logger.warning("trajectory screenshot persistence failed for %s: %s", role_name, exc)
        trajectory_artifacts = {
            "normalized_trajectory": "",
            "screenshot_manifest": "",
            "screenshot_count": 0,
            "total_screenshot_bytes": 0,
            "screenshots": [],
            "persistence_error": str(exc),
        }
    final_screenshot_name = _persist_final_screenshot(
        round_dir=round_dir,
        role_name=role_name,
        payload=final_screenshot,
        trajectory_artifacts=trajectory_artifacts,
    )
    if episode_root is not None:
        try:
            trajectory_artifacts["episode_dir"] = _write_episode_record(
                episode_root=episode_root,
                round_dir=round_dir,
                role_name=role_name,
                result=result,
                trajectory_artifacts=trajectory_artifacts,
                final_screenshot_name=final_screenshot_name,
            )
        except (OSError, ValueError) as exc:
            logger.warning("episode record persistence failed for %s: %s", role_name, exc)
            trajectory_artifacts["episode_persistence_error"] = str(exc)
    metadata = {
        "status": result.status,
        "error": result.error,
        "duration_ms": result.duration_ms,
        "metadata": result.metadata,
        "live_trajectory_preserved": preserved_live_trajectory,
        "trajectory_truncated": bool(live_trajectory_truncated or final_trajectory_truncated),
        "trajectory_artifacts": trajectory_artifacts,
    }
    metadata_text = json.dumps(_json_safe(metadata), ensure_ascii=False, indent=2)
    _write_local(round_dir / f"{role_name}_metadata.json", metadata_text)
    return trajectory_artifacts


def _write_episode_record(
    *,
    episode_root: Path,
    round_dir: Path,
    role_name: str,
    result: EpisodeResult,
    trajectory_artifacts: dict[str, Any],
    final_screenshot_name: str,
) -> str:
    """Write the role episode tree used by the OSWorld-V2 CUA-Harness runner."""

    episode_dir = _next_episode_dir(episode_root)
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    visible = _visible_output(result).strip()
    stderr_tail = str(metadata.get("stderr_tail") or "").strip()
    agent_log_parts = [
        f"role={role_name}",
        f"status={result.status}",
        f"duration_ms={result.duration_ms}",
    ]
    if result.error:
        agent_log_parts.extend(("", f"error: {result.error}"))
    if stderr_tail:
        agent_log_parts.extend(("", "stderr:", stderr_tail))
    if visible:
        agent_log_parts.extend(("", "assistant output:", visible))
    _write_local(episode_dir / "agent.log", "\n".join(agent_log_parts).rstrip() + "\n")
    raw_name = f"{role_name}_raw_trajectory.jsonl"
    raw = _read_local_bounded(round_dir / raw_name, _MAX_SAVED_TRAJECTORY_BYTES) or ""
    command_value = metadata.get("command")
    command = (
        " ".join(str(part) for part in command_value)
        if isinstance(command_value, (list, tuple))
        else str(command_value or "")
    )
    stream_name = (
        "claude_stream.jsonl"
        if "claude" in command.lower()
        else "codex_stream.jsonl"
        if "codex" in command.lower()
        else "provider_stream.jsonl"
    )
    _write_local(episode_dir / stream_name, raw)
    _write_local(episode_dir / "chat.jsonl", _episode_chat_jsonl(result))
    episode_metadata = {
        "status": result.status,
        "error": result.error,
        "duration_ms": result.duration_ms,
        "role": role_name,
        "source_round": round_dir.name,
        "metadata": metadata,
    }
    _write_local(
        episode_dir / "metadata.json",
        json.dumps(_json_safe(episode_metadata), ensure_ascii=False, indent=2) + "\n",
    )

    screenshots = trajectory_artifacts.get("screenshots")
    if isinstance(screenshots, list):
        for item in screenshots:
            if not isinstance(item, dict):
                continue
            name = str(item.get("screenshot_file") or "")
            if not name:
                continue
            payload = _read_local_bytes(round_dir / name, 8 * 1024 * 1024)
            if payload is not None:
                _atomic_bytes_write(episode_dir / name, payload)
        for item in reversed(screenshots):
            if not isinstance(item, dict):
                continue
            name = str(item.get("screenshot_file") or "")
            payload = _read_local_bytes(round_dir / name, 8 * 1024 * 1024) if name else None
            if payload is None:
                continue
            suffix = Path(name).suffix.lower() or ".png"
            _atomic_bytes_write(episode_dir / f"final_screenshot{suffix}", payload)
            break
    if final_screenshot_name:
        payload = _read_local_bytes(round_dir / final_screenshot_name, 8 * 1024 * 1024)
        if payload is not None:
            suffix = Path(final_screenshot_name).suffix.lower() or ".png"
            _atomic_bytes_write(episode_dir / f"final_screenshot{suffix}", payload)
    return str(episode_dir)


async def _capture_environment_screenshot(env: Environment) -> bytes | None:
    """Capture the final GUI state for a GUI role, as OSWorld does per episode."""

    try:
        payload = await env.screenshot()
    except Exception as exc:
        logger.warning("final GUI screenshot capture failed: %s", exc)
        return None
    if not payload or len(payload) > 8 * 1024 * 1024 or _image_suffix(payload) is None:
        return None
    return payload


def _persist_final_screenshot(
    *,
    round_dir: Path,
    role_name: str,
    payload: bytes | None,
    trajectory_artifacts: dict[str, Any],
) -> str:
    if not payload:
        return ""
    suffix = _image_suffix(payload)
    if suffix is None or len(payload) > 8 * 1024 * 1024:
        return ""
    name = f"{role_name}_final_screenshot{suffix}"
    _atomic_bytes_write(round_dir / name, payload)
    item = {
        "step_num": None,
        "image_index": 1,
        "screenshot_file": name,
        "media_type": {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }[suffix],
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "kind": "final_environment_screenshot",
    }
    screenshots = trajectory_artifacts.get("screenshots")
    if not isinstance(screenshots, list):
        screenshots = []
        trajectory_artifacts["screenshots"] = screenshots
    screenshots.append(item)
    trajectory_artifacts["screenshot_count"] = len(screenshots)
    trajectory_artifacts["total_screenshot_bytes"] = int(
        trajectory_artifacts.get("total_screenshot_bytes") or 0
    ) + len(payload)
    manifest_name = str(trajectory_artifacts.get("screenshot_manifest") or "")
    if manifest_name:
        manifest = {
            "schema_version": 1,
            "role": role_name,
            "trajectory_file": str(trajectory_artifacts.get("normalized_trajectory") or ""),
            "live": False,
            "screenshot_count": trajectory_artifacts["screenshot_count"],
            "total_screenshot_bytes": trajectory_artifacts["total_screenshot_bytes"],
            "screenshots": screenshots,
        }
        _write_local(
            round_dir / manifest_name,
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
    return name


def _image_suffix(payload: bytes) -> str | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return ".webp"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    return None


def _episode_chat_jsonl(result: EpisodeResult) -> str:
    """Materialize a provider-neutral OpenClaw-v3-style visible chat ledger."""

    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    raw = result.actions_log or ""
    texts = decode_agent_assistant_texts(raw)
    if not texts:
        visible = _visible_output(result).strip()
        if visible:
            texts = [visible]
    records: list[dict[str, Any]] = [
        {
            "type": "session",
            "version": 3,
            "id": "chat",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "cwd": str(metadata.get("workspace") or ""),
        }
    ]
    for text in texts:
        records.append(
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
    return "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n"


def _next_episode_dir(episode_root: Path) -> Path:
    _ensure_dir_nofollow(episode_root)
    highest = 0
    try:
        entries = episode_root.iterdir()
    except OSError as exc:
        raise ValueError(f"cannot scan episode root: {episode_root}") from exc
    try:
        for index, entry in enumerate(entries):
            if index >= 10_000:
                raise ValueError(f"too many episode entries: {episode_root}")
            name = entry.name
            if len(name) == 5 and name.startswith("ep") and name[2:].isdigit():
                highest = max(highest, int(name[2:]))
    except OSError as exc:
        raise ValueError(f"cannot scan episode root: {episode_root}") from exc
    episode_dir = episode_root / f"ep{highest + 1:03d}"
    if episode_dir.exists() or episode_dir.is_symlink():
        raise ValueError(f"episode path already exists: {episode_dir}")
    _ensure_dir_nofollow(episode_dir)
    return episode_dir


def _read_local_bytes(path: Path, max_bytes: int) -> bytes | None:
    fd: int | None = None
    try:
        fd = _open_nofollow(path)
        metadata = os.fstat(fd)
        if (
            not stat_module.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > max_bytes
        ):
            return None
        chunks: list[bytes] = []
        remaining = int(metadata.st_size)
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _merge_episode_logs(log_dir: Path) -> None:
    """Create OSWorld-style task-level ``agent.log`` and ``chat.jsonl`` files."""

    episode_roots = (
        "manager_episodes",
        "gui_executor_episodes",
        "cli_executor_episodes",
        "gui_auditor_episodes",
        "cli_auditor_episodes",
        "final_response_episodes",
    )
    agent_sections: list[str] = []
    chat_lines: list[str] = []
    total_chars = 0
    total_chat_chars = 0
    max_chars = 64 * 1024 * 1024
    for root_name in episode_roots:
        root = log_dir / root_name
        if not root.is_dir() or root.is_symlink():
            continue
        try:
            entries: list[Path] = []
            for index, entry in enumerate(root.iterdir()):
                if index >= 1_000:
                    break
                entries.append(entry)
            entries.sort(key=lambda path: path.name)
        except OSError:
            continue
        for episode_dir in entries:
            if not episode_dir.is_dir() or episode_dir.is_symlink():
                continue
            agent_text = _read_local_bounded(episode_dir / "agent.log", _MAX_SAVED_TRAJECTORY_BYTES) or ""
            section = f"\n===== {root_name}/{episode_dir.name} agent.log =====\n{agent_text}\n"
            if total_chars + len(section) > max_chars:
                break
            agent_sections.append(section)
            total_chars += len(section)
            chat_text = _read_local_bounded(episode_dir / "chat.jsonl", _MAX_SAVED_TRAJECTORY_BYTES) or ""
            for line in chat_text.splitlines():
                if not line.strip():
                    continue
                if total_chat_chars + len(line) + 1 > max_chars:
                    break
                chat_lines.append(line)
                total_chat_chars += len(line) + 1
    result_dir = log_dir.parent
    _write_local(result_dir / "agent.log", "".join(agent_sections))
    _write_local(result_dir / "chat.jsonl", ("\n".join(chat_lines) + "\n") if chat_lines else "")


def _bounded_text_tail(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text, False
    return raw[-max_bytes:].decode("utf-8", errors="replace"), True


def _read_local_text_tail(path: Path, max_bytes: int) -> tuple[str, bool]:
    """Read at most the latest ``max_bytes`` from a live role trajectory."""

    try:
        fd = _open_nofollow(path)
        try:
            metadata = os.fstat(fd)
            if not stat_module.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                return "", False
            size = int(metadata.st_size)
            truncated = size > max_bytes
            if truncated:
                os.lseek(fd, -max_bytes, os.SEEK_END)
            else:
                os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, max_bytes)
        finally:
            os.close(fd)
    except OSError:
        return "", False
    return raw.decode("utf-8", errors="replace"), truncated


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
    _append_jsonl_nofollow(rounds_jsonl, asdict(record))
    await _write_remote_round_text(env, config, record.round_index, "round.json", payload)
    _append_event(events_path, "managed_round_recorded", asdict(record))


async def _ensure_remote_layout(env: Environment, config: HarnessConfig) -> None:
    # The remote layout is intentionally small: final report plus per-round role
    # artifacts under `.harness/orchestration`.
    harness_dir = config.harness_dir.rstrip("/")
    for path in (
        harness_dir,
        f"{harness_dir}/orchestration",
        f"{harness_dir}/orchestration/rounds",
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
    remote_dir = f"{config.harness_dir.rstrip('/')}/orchestration/rounds/round_{round_index:03d}"
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
    _atomic_bytes_write(path, text.encode("utf-8"))


def _budget_to_dict(budget: EpisodeBudget) -> dict[str, int]:
    return {
        "max_duration_seconds": budget.max_duration_seconds,
    }


def _append_event(path: Path, event: str, payload: dict[str, Any]) -> None:
    _ensure_dir_nofollow(path.parent)
    # Event ids are assigned while holding the file lock, so the same absolute
    # id survives snapshot truncation, REST replay, and a reconnect after an
    # API restart.  The legacy ``event`` field remains for old readers.
    parent_fd: int | None = None
    raw_fd: int | None = None
    try:
        parent_fd = _open_nofollow(path.parent, directory=True)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise OSError("secure event append requires O_NOFOLLOW")
        raw_fd = os.open(
            path.name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_APPEND
            | nofollow
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(raw_fd)
        if not stat_module.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("event log is not a private regular file")
        fh = os.fdopen(raw_fd, "a+", encoding="utf-8")
        raw_fd = None
        with fh:
            flock = None
            try:
                import fcntl

                flock = fcntl
                flock.flock(fh.fileno(), flock.LOCK_EX)
            except (ImportError, OSError):
                pass
            try:
                fh.seek(0)
                sequence = sum(1 for line in fh if line.strip()) + 1
                fh.seek(0, 2)
                run_id = path.parents[2].name if len(path.parents) > 2 else "local"
                record = {
                    "schema_version": 1,
                    "event_id": f"{run_id}:{sequence:06d}",
                    "ts": time.time(),
                    "event": event,
                    **_json_safe(payload),
                }
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            finally:
                if flock is not None:
                    try:
                        flock.flock(fh.fileno(), flock.LOCK_UN)
                    except OSError:
                        pass
    finally:
        if raw_fd is not None:
            try:
                os.close(raw_fd)
            except OSError:
                pass
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
