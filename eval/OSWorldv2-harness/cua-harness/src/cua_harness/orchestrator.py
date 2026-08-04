from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .adapters.base import AgentAdapter
from .environment.base import Environment
from .remote_io import ensure_remote_dir, write_remote_text
from .role_prompts import (
    ORCHESTRATED_NEXT_BLOCKED,
    ORCHESTRATED_NEXT_DONE,
    ORCHESTRATED_NEXT_GUI,
    ORCHESTRATED_NEXT_INVALID,
    build_role_orchestrator_prompt,
    build_role_task_prompt,
    build_role_verifier_format_repair_prompt,
    build_role_verifier_prompt,
    extract_role_orchestrator_plan_text,
    extract_related_report_refs,
    extract_role_task_contract,
    extract_role_task_state,
    format_related_verifier_reports,
    format_orchestration_history,
    parse_role_orchestrator_next_step,
)
from .types import (
    EpisodeBudget,
    EpisodeResult,
    HarnessConfig,
    OrchestratedRound,
    RoleNextStep,
)
from .verifier_agent import (
    has_valid_verifier_control_header,
    parse_verify_report,
    verifier_report_text_from_episode_result,
    verify_report_from_episode_result,
)

ROLE_VARIANT = "cua_harness_role_orchestrated"
logger = logging.getLogger(__name__)


async def run(
    *,
    task: str,
    env: Environment,
    agent: AgentAdapter,
    config: HarnessConfig,
    verifier_agent: AgentAdapter | None = None,
    orchestrator_agent: AgentAdapter | None = None,
    gui_task_agent: AgentAdapter | None = None,
    cli_task_agent: AgentAdapter | None = None,
    gui_verifier_agent: AgentAdapter | None = None,
    cli_verifier_agent: AgentAdapter | None = None,
) -> dict[str, Any]:
    """Run the generic CUA-Harness four-role orchestration loop.

    The default `agent` can back every role, which is how Codex or Claude Code
    adapters start. Callers with stronger role controls can pass distinct
    adapters for orchestrator, GUI task, CLI task, GUI verifier, and CLI verifier.
    """

    # Role binding is resolved once at startup so the main loop can stay focused
    # on state transitions instead of adapter fallback logic.
    orchestrator_agent = orchestrator_agent or agent
    gui_task_agent = gui_task_agent or agent
    cli_task_agent = cli_task_agent or agent
    gui_verifier_agent = gui_verifier_agent or verifier_agent or agent
    cli_verifier_agent = cli_verifier_agent or verifier_agent or agent

    # Budget aliases let older wrappers pass episode/verifier budgets while the
    # role runner still exposes separate knobs for newer integrations.
    orchestrator_budget = config.orchestrator_budget or config.verifier_budget
    gui_task_budget = config.gui_task_budget or config.episode_budget
    cli_task_budget = config.cli_task_budget or config.episode_budget
    verifier_budget = config.role_verifier_budget or config.verifier_budget

    log_dir = Path(config.log_dir)
    role_dir = log_dir / "role_orchestration"
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
            "orchestrator_budget": _budget_to_dict(orchestrator_budget),
            "gui_task_budget": _budget_to_dict(gui_task_budget),
            "cli_task_budget": _budget_to_dict(cli_task_budget),
            "verifier_budget": _budget_to_dict(verifier_budget),
        },
    )

    rounds: list[OrchestratedRound] = []
    abort_reason = ""
    completion_satisfied = False
    last_plan = ""
    current_task_state = ""
    current_task_contract = ""

    for round_index in range(1, max(1, config.max_total_episodes) + 1):
        round_dir = rounds_dir / f"round_{round_index:03d}"
        round_dir.mkdir(parents=True, exist_ok=True)

        # The orchestrator sees the original task, its maintained task state,
        # stable task contract, and verifier reports. It never receives raw
        # trajectories or previous full prompts.
        orchestrator_prompt = build_role_orchestrator_prompt(
            task=task,
            rounds=rounds,
            round_index=round_index,
            task_state=current_task_state,
            task_contract=current_task_contract,
            max_history_chars=config.role_history_chars,
        )
        _write_local(round_dir / "orchestrator_input.txt", orchestrator_prompt)
        await _write_remote_round_text(env, config, round_index, "orchestrator_input.txt", orchestrator_prompt)
        _append_event(
            events_path,
            "orchestrator_round_start",
            {"round": round_index, "prompt_chars": len(orchestrator_prompt)},
        )

        orchestrator_result = await _run_orchestrator_with_quiet_retries(
            env=env,
            agent=orchestrator_agent,
            budget=orchestrator_budget,
            prompt=orchestrator_prompt,
            round_dir=round_dir,
            events_path=events_path,
            round_index=round_index,
        )
        plan_text = extract_role_orchestrator_plan_text(_visible_output(orchestrator_result)).strip()
        if not plan_text:
            plan_text = "下一步: 阻塞\n\n阻塞原因:\n编排器没有产生可读取的自然语言输出。"
        current_task_state = extract_role_task_state(plan_text, fallback=current_task_state)
        current_task_contract = extract_role_task_contract(plan_text, fallback=current_task_contract)
        related_report_refs = extract_related_report_refs(plan_text)
        _write_local(round_dir / "orchestrator_plan.txt", plan_text)
        _write_local(round_dir / "task_state.txt", current_task_state)
        _write_local(round_dir / "task_contract.txt", current_task_contract)
        await _write_remote_round_text(env, config, round_index, "orchestrator_plan.txt", plan_text)
        await _write_remote_round_text(env, config, round_index, "task_state.txt", current_task_state)
        await _write_remote_round_text(env, config, round_index, "task_contract.txt", current_task_contract)

        next_step = parse_role_orchestrator_next_step(plan_text)
        last_plan = plan_text
        _append_event(
            events_path,
            "orchestrator_round_done",
            {
                "round": round_index,
                "next_step": next_step,
                "plan_chars": len(plan_text),
                "task_state_chars": len(current_task_state),
                "task_contract_chars": len(current_task_contract),
                "related_report_refs": related_report_refs,
                "status": _episode_status(orchestrator_result),
            },
        )

        if next_step == ORCHESTRATED_NEXT_DONE:
            unfinished_items = _unfinished_task_state_items(current_task_state)
            if _latest_verifier_is_clean_complete(rounds) and not unfinished_items:
                completion_satisfied = True
                rounds.append(
                    OrchestratedRound(
                        round_index=round_index,
                        next_step=next_step,
                        plan_text=plan_text,
                        task_state=current_task_state,
                        task_contract=current_task_contract,
                        related_report_refs=related_report_refs,
                    )
                )
                await _record_round(env, config, role_dir, events_path, rounds[-1])
                break

            # Completion is not accepted unless it is grounded in a previous
            # clean verifier report. The synthetic audit gets fed back into the
            # next orchestrator turn as a repair signal.
            repair_report = _invalid_completion_report(
                latest_clean_complete=_latest_verifier_is_clean_complete(rounds),
                unfinished_items=unfinished_items,
            )
            record = OrchestratedRound(
                round_index=round_index,
                next_step=ORCHESTRATED_NEXT_INVALID,
                plan_text=plan_text,
                harness_feedback=repair_report,
                task_state=current_task_state,
                task_contract=current_task_contract,
                related_report_refs=related_report_refs,
                verifier_status={"invalid_completion": True},
            )
            _write_local(round_dir / "harness_feedback.txt", repair_report)
            await _write_remote_round_text(env, config, round_index, "harness_feedback.txt", repair_report)
            rounds.append(record)
            await _record_round(env, config, role_dir, events_path, record)
            continue

        if next_step == ORCHESTRATED_NEXT_BLOCKED:
            abort_reason = "orchestrator_blocked"
            rounds.append(
                OrchestratedRound(
                    round_index=round_index,
                    next_step=next_step,
                    plan_text=plan_text,
                    task_state=current_task_state,
                    task_contract=current_task_contract,
                    related_report_refs=related_report_refs,
                )
            )
            await _record_round(env, config, role_dir, events_path, rounds[-1])
            break

        if next_step == ORCHESTRATED_NEXT_INVALID:
            # Bad route output is treated like a verifier finding so the next
            # orchestrator turn has an explicit, auditable correction signal.
            repair_report = (
                "状态: incomplete\n"
                "完整性: suspect\n"
                "契约审计: unknown\n"
                "审计事实: 编排器输出没有使用规定的第一行路由标记，无法分配给 GUI 或 CLI task agent。\n"
                "缺口: 编排器必须重新输出一个主目标形态明确的 GUI 或 CLI 子任务，或明确完成/阻塞。\n"
                "下一步: 重新编排，第一行必须是 `下一步: GUI任务`、`下一步: CLI任务`、`下一步: 完成` 或 `下一步: 阻塞`。"
            )
            record = OrchestratedRound(
                round_index=round_index,
                next_step=ORCHESTRATED_NEXT_INVALID,
                plan_text=plan_text,
                harness_feedback=repair_report,
                task_state=current_task_state,
                task_contract=current_task_contract,
                related_report_refs=related_report_refs,
                verifier_status={"invalid_plan": True},
            )
            _write_local(round_dir / "harness_feedback.txt", repair_report)
            await _write_remote_round_text(env, config, round_index, "harness_feedback.txt", repair_report)
            rounds.append(record)
            await _record_round(env, config, role_dir, events_path, record)
            continue

        task_agent, task_budget = _task_binding(
            next_step=next_step,
            gui_task_agent=gui_task_agent,
            cli_task_agent=cli_task_agent,
            gui_task_budget=gui_task_budget,
            cli_task_budget=cli_task_budget,
        )
        verifier_for_step = gui_verifier_agent if next_step == ORCHESTRATED_NEXT_GUI else cli_verifier_agent
        related_verifier_reports = format_related_verifier_reports(
            rounds,
            related_report_refs,
            max_chars=config.role_verified_context_chars,
        )

        # Task prompts receive the orchestrator-maintained state plus only the
        # verifier reports explicitly referenced by the current subtask contract.
        task_prompt = build_role_task_prompt(
            task=task,
            rounds=rounds,
            plan_text=plan_text,
            next_step=next_step,
            task_state=current_task_state,
            task_contract=current_task_contract,
            related_verifier_reports=related_verifier_reports,
        )
        _write_local(round_dir / "task_prompt.txt", task_prompt)
        await _write_remote_round_text(env, config, round_index, "task_prompt.txt", task_prompt)
        _append_event(
            events_path,
            "task_role_start",
            {"round": round_index, "role": next_step, "prompt_chars": len(task_prompt), "budget": _budget_to_dict(task_budget)},
        )

        task_result = await task_agent.run_episode(task_prompt, env, task_budget)
        _save_role_result(round_dir, "task", task_result)
        task_output = _visible_output(task_result).strip() or "(task agent produced no readable natural-language output)"
        _write_local(round_dir / "task_output.txt", task_output)
        await _write_remote_round_text(env, config, round_index, "task_output.txt", task_output)
        _append_event(
            events_path,
            "task_role_done",
            {
                "round": round_index,
                "role": next_step,
                "output_chars": len(task_output),
                "status": _episode_status(task_result),
            },
        )

        # The verifier audits only the just-finished subtask. Its natural
        # language report becomes the trusted intermediate state for later rounds.
        verifier_prompt = build_role_verifier_prompt(
            task=task,
            rounds=rounds,
            plan_text=plan_text,
            task_output=task_output,
            next_step=next_step,
            task_state=current_task_state,
            task_contract=current_task_contract,
            related_verifier_reports=related_verifier_reports,
            max_task_output_chars=config.verifier_task_output_chars,
        )
        _write_local(round_dir / "verifier_input.txt", verifier_prompt)
        await _write_remote_round_text(env, config, round_index, "verifier_input.txt", verifier_prompt)
        _append_event(
            events_path,
            "verifier_role_start",
            {"round": round_index, "role": next_step, "prompt_chars": len(verifier_prompt), "budget": _budget_to_dict(verifier_budget)},
        )

        verifier_result = await verifier_for_step.run_episode(verifier_prompt, env, verifier_budget)
        _save_role_result(round_dir, "verifier", verifier_result)
        verifier_report, verifier_status = await _verifier_report_with_format_repair(
            env=env,
            config=config,
            round_dir=round_dir,
            events_path=events_path,
            verifier_agent=verifier_for_step,
            verifier_budget=verifier_budget,
            primary_result=verifier_result,
            round_index=round_index,
        )
        _write_local(round_dir / "verifier_report.txt", verifier_report)
        await _write_remote_round_text(env, config, round_index, "verifier_report.txt", verifier_report)

        record = OrchestratedRound(
            round_index=round_index,
            next_step=next_step,
            plan_text=plan_text,
            task_output=task_output,
            verifier_report=verifier_report,
            task_state=current_task_state,
            task_contract=current_task_contract,
            related_report_refs=related_report_refs,
            task_status=_episode_status(task_result),
            verifier_status=verifier_status,
        )
        rounds.append(record)
        await _record_round(env, config, role_dir, events_path, record)
        _append_event(
            events_path,
            "verifier_role_done",
            {
                "round": round_index,
                "role": next_step,
                "report_chars": len(verifier_report),
                "status": _episode_status(verifier_result),
            },
        )
    else:
        abort_reason = "max_rounds_exhausted"

    elapsed = time.monotonic() - started
    final = _final_report(
        task=task,
        rounds=rounds,
        completion_satisfied=completion_satisfied,
        abort_reason=abort_reason,
        last_plan=last_plan,
        task_state=current_task_state,
        task_contract=current_task_contract,
        max_rounds=max(1, config.max_total_episodes),
        elapsed_seconds=elapsed,
    )
    _write_local(role_dir / "report.json", json.dumps(final, ensure_ascii=False, indent=2) + "\n")
    _write_local(log_dir / "report.json", json.dumps(final, ensure_ascii=False, indent=2) + "\n")
    transcript = format_orchestration_history(rounds, include_empty=True, max_chars=200_000)
    _write_local(role_dir / "orchestration_transcript.txt", transcript)
    await _write_remote_text(env, f"{config.harness_dir.rstrip('/')}/report.json", json.dumps(final, ensure_ascii=False, indent=2))
    await _write_remote_text(
        env,
        f"{config.harness_dir.rstrip('/')}/orchestration/report.json",
        json.dumps(final, ensure_ascii=False, indent=2),
    )
    await _write_remote_text(env, f"{config.harness_dir.rstrip('/')}/orchestration/orchestration_transcript.txt", transcript)
    _append_event(events_path, "role_harness_done", final)
    return final


def _task_binding(
    *,
    next_step: RoleNextStep,
    gui_task_agent: AgentAdapter,
    cli_task_agent: AgentAdapter,
    gui_task_budget: EpisodeBudget,
    cli_task_budget: EpisodeBudget,
) -> tuple[AgentAdapter, EpisodeBudget]:
    if next_step == ORCHESTRATED_NEXT_GUI:
        return gui_task_agent, gui_task_budget
    return cli_task_agent, cli_task_budget


async def _run_orchestrator_with_quiet_retries(
    *,
    env: Environment,
    agent: AgentAdapter,
    budget: EpisodeBudget,
    prompt: str,
    round_dir: Path,
    events_path: Path,
    round_index: int,
) -> EpisodeResult:
    max_attempts = max(1, _env_int("CUA_HARNESS_ORCHESTRATOR_QUIET_RETRY_ATTEMPTS", 3))
    sleep_seconds = max(0.0, _env_float("CUA_HARNESS_ORCHESTRATOR_QUIET_RETRY_SLEEP_SECONDS", 60.0))
    last_result: EpisodeResult | None = None
    last_reason = ""

    for attempt in range(1, max_attempts + 1):
        result = await agent.run_episode(prompt, env, budget)
        last_result = result
        _save_role_result(round_dir, f"orchestrator_attempt_{attempt:02d}", result)
        visible = _visible_output(result)
        plan_text = extract_role_orchestrator_plan_text(visible).strip()
        reason = _transient_orchestrator_failure_reason(result, visible, plan_text)
        last_reason = reason
        _append_event(
            events_path,
            "orchestrator_attempt_done",
            {
                "round": round_index,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "status": _episode_status(result),
                "visible_chars": len(visible.strip()),
                "plan_chars": len(plan_text),
                "transient_reason": reason,
            },
        )
        if not reason:
            _save_role_result(round_dir, "orchestrator", result)
            return result
        if attempt < max_attempts and sleep_seconds > 0:
            _append_event(
                events_path,
                "orchestrator_quiet_retry_sleep",
                {
                    "round": round_index,
                    "attempt": attempt,
                    "reason": reason,
                    "sleep_seconds": sleep_seconds,
                },
            )
            await asyncio.sleep(sleep_seconds)

    assert last_result is not None
    _append_event(
        events_path,
        "orchestrator_quiet_retries_exhausted",
        {
            "round": round_index,
            "attempts": max_attempts,
            "last_reason": last_reason,
        },
    )
    _save_role_result(round_dir, "orchestrator", last_result)
    return last_result


def _transient_orchestrator_failure_reason(result: EpisodeResult, visible_output: str, plan_text: str) -> str:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    if result.status != "done":
        return result.status
    if metadata.get("timed_out"):
        return "timed_out"
    if metadata.get("agent_done") is False:
        return "agent_not_done"
    if metadata.get("actions_log_diagnostics_only"):
        return "diagnostics_only"
    visible = visible_output.strip()
    if not visible:
        return "empty_output"
    if _looks_like_runtime_diagnostics(visible) and parse_role_orchestrator_next_step(plan_text) == ORCHESTRATED_NEXT_INVALID:
        return "runtime_diagnostics_output"
    return ""


def _looks_like_runtime_diagnostics(text: str) -> bool:
    sample = text.strip()[:4000]
    return any(
        marker in sample
        for marker in (
            "CLAUDE_CODE_DEBUG=",
            "RETRY_DECISION:",
            "NO_RESULT",
            "SILENT_FAIL",
            "MAX_STEPS_REACHED=0 timed_out_after=",
            "PROVIDER_400 detected",
            "UPSTREAM_ERROR detected",
        )
    )


async def _verifier_report_with_format_repair(
    *,
    env: Environment,
    config: HarnessConfig,
    round_dir: Path,
    events_path: Path,
    verifier_agent: AgentAdapter,
    verifier_budget: EpisodeBudget,
    primary_result: EpisodeResult,
    round_index: int,
) -> tuple[str, dict[str, Any]]:
    status = _episode_status(primary_result)
    raw_report = verifier_report_text_from_episode_result(primary_result)
    if not _should_repair_verifier_format(primary_result, raw_report):
        return _verifier_report_text(primary_result, round_index), status

    repair_prompt = build_role_verifier_format_repair_prompt(report_text=raw_report)
    _write_local(round_dir / "verifier_format_repair_input.txt", repair_prompt)
    await _write_remote_round_text(env, config, round_index, "verifier_format_repair_input.txt", repair_prompt)
    repair_budget = _format_repair_budget(verifier_budget)
    _append_event(
        events_path,
        "verifier_format_repair_start",
        {
            "round": round_index,
            "prompt_chars": len(repair_prompt),
            "budget": _budget_to_dict(repair_budget),
        },
    )
    repair_result = await verifier_agent.run_episode(repair_prompt, env, repair_budget)
    _save_role_result(round_dir, "verifier_format_repair", repair_result)
    repair_raw_report = verifier_report_text_from_episode_result(repair_result)
    repair_valid = _should_accept_verifier_format_repair(repair_result, repair_raw_report)
    status = {
        **status,
        "format_repair_attempted": True,
        "format_repair_accepted": repair_valid,
        "format_repair_status": _episode_status(repair_result),
    }
    _append_event(
        events_path,
        "verifier_format_repair_done",
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
        return _verifier_report_text(corrected, round_index), status
    return _verifier_report_text(repair_result, round_index), status


def _should_repair_verifier_format(result: EpisodeResult, report_text: str) -> bool:
    if result.status != "done":
        return False
    if _runtime_signal_labels(result):
        return False
    return not has_valid_verifier_control_header(report_text)


def _should_accept_verifier_format_repair(result: EpisodeResult, report_text: str) -> bool:
    if result.status != "done":
        return False
    if _runtime_signal_labels(result):
        return False
    if _workspace_mutation_detected(result):
        return False
    return has_valid_verifier_control_header(report_text)


def _format_repair_budget(budget: EpisodeBudget) -> EpisodeBudget:
    return EpisodeBudget(
        max_turns=max(1, min(budget.max_turns, 3)),
        max_duration_seconds=max(30, min(budget.max_duration_seconds, 120)),
    )


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        logger.warning("invalid %s=%r; using %s", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        logger.warning("invalid %s=%r; using %s", name, raw, default)
        return default


def _verifier_report_text(result: EpisodeResult, round_index: int) -> str:
    report = verify_report_from_episode_result(result, round_index)
    if report.report_text.strip():
        return report.report_text.strip()
    visible = _visible_output(result).strip()
    if visible:
        return visible
    return (
        "状态: blocked\n"
        "完整性: suspect\n"
        "契约审计: unknown\n"
        "审计事实: verifier 没有产生可读取的自然语言审计报告。\n"
        "下一步: 编排器应重试审计或生成更小的同类型子任务。"
    )


def _latest_verifier_is_clean_complete(rounds: list[OrchestratedRound]) -> bool:
    for item in reversed(rounds):
        if item.verifier_status.get("invalid_completion") or item.verifier_status.get("invalid_plan"):
            continue
        if not item.verifier_report.strip():
            continue
        report = parse_verify_report(item.verifier_report, item.round_index)
        return (
            report.status == "complete"
            and report.integrity_status == "clean"
            and report.contract_audit_status == "aligned"
        )
    return False


_UNFINISHED_HEADER_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:未完成|待完成|还缺|缺少|缺口|missing|incomplete)\s*[:：]\s*(.*)$"
)
_TASK_STATE_SECTION_BOUNDARY_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:已完成|完成|阻塞|风险|阻塞/风险|不可信|不可复用|不可信/不可复用|任务契约|依赖判断|下一步|任务|验收标准|边界|completed|done|blocked|risk|integrity)\s*[:：]"
)
_NO_UNFINISHED_RE = re.compile(
    r"(?ix)^\s*"
    r"[\(\[（【]*\s*"
    r"(?:"
    r"无(?:任何|其他|其它)?(?:未完成|待完成|剩余|缺口|缺少|事项|任务|内容|工作)?"
    r"|没有(?:任何|其他|其它)?(?:未完成|待完成|剩余|缺口|缺少|事项|任务|内容|工作)?"
    r"|暂无(?:任何|其他|其它)?(?:未完成|待完成|剩余|缺口|缺少|事项|任务|内容|工作)?"
    r"|全部完成"
    r"|none|nothing|n/?a"
    r"|no\s+(?:missing|unfinished|remaining(?:\s+work)?)"
    r")"
    r"(?:\s*[\)）\]】])?"
    r"(?:\s*(?:[。．.，,、；;:：]|--|[-—–/]|[（(]).*)?"
    r"\s*$"
)


def _unfinished_task_state_items(task_state: str) -> list[str]:
    text = str(task_state or "")
    findings: list[str] = []
    for match in _UNFINISHED_HEADER_RE.finditer(text):
        boundary = _TASK_STATE_SECTION_BOUNDARY_RE.search(text, match.end())
        section = text[match.start() : boundary.start() if boundary else len(text)]
        items = _meaningful_unfinished_lines(section)
        if items:
            findings.extend(items)
    return findings[:8]


def _meaningful_unfinished_lines(section: str) -> list[str]:
    lines = []
    for raw_line in section.splitlines():
        line = raw_line.strip().strip("*").strip()
        line = re.sub(r"^(?:[-*+]\s*|\d+[.)]\s*)", "", line).strip()
        line = re.sub(r"^(?:未完成|待完成|还缺|缺少|缺口|missing|incomplete)\s*[:：]\s*", "", line, flags=re.I).strip()
        normalized_line = line.strip("`").strip()
        if not normalized_line or _NO_UNFINISHED_RE.match(normalized_line):
            continue
        lines.append(line)
    return lines


def _invalid_completion_report(*, latest_clean_complete: bool, unfinished_items: list[str]) -> str:
    if unfinished_items:
        missing = "\n".join(f"- {item}" for item in unfinished_items)
        return (
            "状态: incomplete\n"
            "完整性: suspect\n"
            "契约审计: unknown\n"
            "审计事实: 编排器请求完成，但当前任务状态的 `未完成` 或 `缺口` 段仍包含实质缺口。\n"
            f"缺口:\n{missing}\n"
            "下一步: 重新编排，必须先分配 GUI/CLI 子任务完成这些缺口；"
            "在 `未完成` 段清空或明确为“无”之前不能输出 `下一步: 完成`。"
        )
    if latest_clean_complete:
        return (
            "状态: incomplete\n"
            "完整性: suspect\n"
            "契约审计: unknown\n"
            "审计事实: 编排器请求完成，但完成 guard 未能确认当前任务状态已完全闭合。\n"
            "缺口: 必须先让 verifier 明确确认所有原始要求已完成，且 `未完成` 段为空或明确为“无”。\n"
            "下一步: 重新编排，补齐缺口后再请求完成。"
        )
    return (
        "状态: incomplete\n"
        "完整性: suspect\n"
        "契约审计: unknown\n"
        "审计事实: 编排器请求完成，但最近 verifier 报告没有明确确认所有原始要求 complete、clean 且契约审计 aligned。\n"
        "缺口: 必须先分配一个可验证的 GUI/CLI 子任务，或等待 verifier 明确确认完成。\n"
        "下一步: 重新编排，除非已有 verifier complete/clean/aligned 证据，否则不能输出 `下一步: 完成`。"
    )


def _final_report(
    *,
    task: str,
    rounds: list[OrchestratedRound],
    completion_satisfied: bool,
    abort_reason: str,
    last_plan: str,
    task_state: str,
    task_contract: str,
    max_rounds: int,
    elapsed_seconds: float,
) -> dict[str, Any]:
    # Final status is a harness-level decision, not the last task agent's self
    # claim. The verifier artifact remains the natural-language audit report.
    latest_report_text = _latest_verifier_report_text(rounds)
    status = "complete" if completion_satisfied else "blocked" if abort_reason == "orchestrator_blocked" else "incomplete"
    return {
        "schema_version": 2,
        "variant": ROLE_VARIANT,
        "mode": "role_orchestration",
        "status": status,
        "task": task,
        "completion_satisfied": completion_satisfied,
        "completion_authority": "orchestrator_with_role_verifiers",
        "rounds_run": len(rounds),
        "max_rounds": max_rounds,
        "abort_reason": abort_reason,
        "last_plan": last_plan,
        "current_task_state": task_state,
        "current_task_contract": task_contract,
        "latest_verifier_report": latest_report_text,
        "rounds": [asdict(item) for item in rounds],
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


def _latest_verifier_report_text(rounds: list[OrchestratedRound]) -> str:
    # Round state intentionally stores verifier reports as natural language. The
    # parser is only a transient stop-condition check.
    for item in reversed(rounds):
        if item.verifier_status.get("invalid_completion") or item.verifier_status.get("invalid_plan"):
            continue
        if item.verifier_report.strip():
            return item.verifier_report.strip()
    return ""


def _visible_output(result: EpisodeResult) -> str:
    # Adapters can expose a clean assistant-visible output in metadata. Falling
    # back to actions_log keeps simple command adapters usable.
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    for key in ("task_agent_visible_output", "visible_task_output", "assistant_visible_output", "output_text"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if metadata.get("actions_log_diagnostics_only"):
        return ""
    return result.actions_log or ""


def _episode_status(result: EpisodeResult) -> dict[str, Any]:
    # Keep status compact in round records; full raw output is stored separately.
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    return {
        "status": result.status,
        "error": result.error,
        "duration_ms": result.duration_ms,
        "agent_done": metadata.get("agent_done"),
        "steps_capped": metadata.get("steps_capped"),
        "exit_code": metadata.get("exit_code"),
        "runtime_signals": metadata.get("runtime_signals"),
    }


def _runtime_signal_labels(result: EpisodeResult) -> list[str]:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    raw = metadata.get("runtime_signals")
    if not isinstance(raw, list):
        return []
    labels: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            signal = item.get("signal")
            if isinstance(signal, str) and signal.strip():
                labels.append(signal.strip())
        elif isinstance(item, str) and item.strip():
            labels.append(item.strip())
    return labels


def _workspace_mutation_detected(result: EpisodeResult) -> bool:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    return bool(metadata.get("verifier_workspace_mutation_detected"))


def _save_role_result(round_dir: Path, role_name: str, result: EpisodeResult) -> None:
    # Raw trajectories are stored locally for audit/debugging, while prompt
    # construction only consumes visible output and verifier reports.
    _write_local(round_dir / f"{role_name}_raw_trajectory.txt", result.actions_log or "")
    metadata = {
        "status": result.status,
        "error": result.error,
        "duration_ms": result.duration_ms,
        "metadata": result.metadata,
    }
    _write_local(round_dir / f"{role_name}_metadata.json", json.dumps(_json_safe(metadata), ensure_ascii=False, indent=2))


async def _record_round(
    env: Environment,
    config: HarnessConfig,
    role_dir: Path,
    events_path: Path,
    record: OrchestratedRound,
) -> None:
    # rounds.jsonl is the append-only local ledger; round.json mirrors the same
    # state into the task VM for later inspection.
    payload = json.dumps(asdict(record), ensure_ascii=False, indent=2)
    rounds_jsonl = role_dir / "rounds.jsonl"
    with rounds_jsonl.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n")
    await _write_remote_round_text(env, config, record.round_index, "round.json", payload)
    _append_event(events_path, "orchestrated_round_recorded", asdict(record))


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _budget_to_dict(budget: EpisodeBudget) -> dict[str, int]:
    return {
        "max_turns": budget.max_turns,
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
