from __future__ import annotations

import re

from .prompt_texts import (
    AUDITOR_CONTRACT_BACKCHECK,
    CLI_AUDITOR_INSTRUCTIONS,
    CLI_EXECUTOR_INSTRUCTIONS,
    FINAL_STATE_SEMANTIC_GUARD,
    FINAL_RESPONSE_INSTRUCTIONS,
    GUI_AUDITOR_INSTRUCTIONS,
    GUI_EXECUTOR_INSTRUCTIONS,
    MANAGER_INSTRUCTIONS,
    TASK_CONTRACT_RULES,
    USER_CLARIFICATION_NOTE,
)
from .types import ManagedRound, PromptLanguage, RoleNextStep

MANAGER_NEXT_GUI: RoleNextStep = "gui"
MANAGER_NEXT_CLI: RoleNextStep = "cli"
MANAGER_NEXT_DONE: RoleNextStep = "done"
MANAGER_NEXT_BLOCKED: RoleNextStep = "blocked"
MANAGER_NEXT_INVALID: RoleNextStep = "invalid"
MANAGER_NEXT_ASK: RoleNextStep = "ask"


def normalize_prompt_language(language: str | None) -> PromptLanguage:
    normalized = str(language or "en").strip().lower()
    if normalized not in {"en", "zh"}:
        raise ValueError(f"Unsupported prompt language: {language!r}; expected 'en' or 'zh'.")
    return normalized  # type: ignore[return-value]


# Backward-compatible public constants now expose the production-default
# English catalog. Runtime builders select either catalog explicitly.
LH_HARNESS_MANAGER_INSTRUCTIONS = MANAGER_INSTRUCTIONS["en"]
LH_HARNESS_GUI_EXECUTOR_INSTRUCTIONS = GUI_EXECUTOR_INSTRUCTIONS["en"]
LH_HARNESS_CLI_EXECUTOR_INSTRUCTIONS = CLI_EXECUTOR_INSTRUCTIONS["en"]
LH_HARNESS_GUI_AUDITOR_INSTRUCTIONS = GUI_AUDITOR_INSTRUCTIONS["en"]
LH_HARNESS_CLI_AUDITOR_INSTRUCTIONS = CLI_AUDITOR_INSTRUCTIONS["en"]


def build_role_manager_prompt(
    *,
    task: str,
    rounds: list[ManagedRound],
    round_index: int,
    task_state: str = "",
    task_contract: str = "",
    language: str = "en",
    max_history_chars: int = 36_000,
) -> str:
    lang = normalize_prompt_language(language)
    auditor_reports = format_verified_intermediate_context(
        rounds, max_chars=max_history_chars, language=lang
    )
    harness_feedback = format_harness_feedback_context(rounds, max_chars=max_history_chars)
    if lang == "en":
        return f"""\
{MANAGER_INSTRUCTIONS[lang].strip()}

Original task:
{task.rstrip()}

Task-contract and final-state rules:
{TASK_CONTRACT_RULES[lang].strip()}

{USER_CLARIFICATION_NOTE[lang].strip()}

Mandatory final-state guard:
{FINAL_STATE_SEMANTIC_GUARD[lang].strip()}

Current stable task contract:
{task_contract.strip() or "(No task contract yet. Initialize it from the original task in this round.)"}

Previous current-task state:
{task_state.strip() or "(No maintained state yet. Initialize it from the original task.)"}

Historical auditor reports by round (authority for trusted intermediate state):
{auditor_reports or "(No auditor reports yet.)"}

Harness management feedback (not an audit; only for protocol/completion correction):
{harness_feedback or "(No harness feedback.)"}

This is management round {round_index}. Output only the next management result.
"""
    return f"""\
{MANAGER_INSTRUCTIONS[lang].strip()}

原始任务:
{task.rstrip()}

任务契约和最终状态规则:
{TASK_CONTRACT_RULES[lang].strip()}

{USER_CLARIFICATION_NOTE[lang].strip()}

必须遵守的最终状态约束:
{FINAL_STATE_SEMANTIC_GUARD[lang].strip()}

当前稳定任务契约:
{task_contract.strip() or "(还没有任务契约；请在本轮根据原始任务初始化。)"}

上一轮当前任务状态:
{task_state.strip() or "(还没有当前任务状态；请根据原始任务初始化。)"}

历史 auditor 报告原文（按 round 编号；可信中间状态的权威来源）:
{auditor_reports or "(还没有 auditor 报告。)"}

harness 任务管理反馈（不是审计，只用于协议或完成请求修正）:
{harness_feedback or "(没有 harness 反馈。)"}

现在是任务管理第 {round_index} 轮。只输出下一步任务管理结果。
"""


def build_role_executor_prompt(
    *,
    task: str,
    plan_text: str,
    next_step: RoleNextStep,
    task_state: str = "",
    task_contract: str = "",
    related_auditor_reports: str = "",
    language: str = "en",
) -> str:
    lang = normalize_prompt_language(language)
    gui = next_step == MANAGER_NEXT_GUI
    role_name = "GUI/visual" if gui and lang == "en" else "CLI/non-GUI" if lang == "en" else "GUI/视觉" if gui else "CLI/非 GUI"
    role_instructions = GUI_EXECUTOR_INSTRUCTIONS[lang] if gui else CLI_EXECUTOR_INSTRUCTIONS[lang]
    if lang == "en":
        return f"""\
{role_instructions.strip()}

Original task:
{task.rstrip()}

Task-contract rules:
{TASK_CONTRACT_RULES[lang].strip()}

{USER_CLARIFICATION_NOTE[lang].strip()}

Final-state guard for this subtask:
{FINAL_STATE_SEMANTIC_GUARD[lang].strip()}

Current task state (manager-maintained; facts must come from auditors):
{task_state.strip() or "(No maintained task state.)"}

Stable task contract:
{task_contract.strip() or "(No separate contract was maintained; use the Task contract section in the assigned plan.)"}

Assigned {role_name} subtask contract:
{plan_text.rstrip()}

Related auditor reports selected by round id:
{related_auditor_reports.strip() or "(The manager referenced no related auditor report.)"}

Complete only this subtask. Treat audited state and the stable contract as the trusted semantic boundary. Do not repeat audited work or use suspect, violating, fabricated, untrusted, or deleted artifacts. If context is missing or another dominant task type is required, stop and report it; do not guess or globally replan.
"""
    return f"""\
{role_instructions.strip()}

原始任务:
{task.rstrip()}

任务契约规则:
{TASK_CONTRACT_RULES[lang].strip()}

{USER_CLARIFICATION_NOTE[lang].strip()}

执行当前子任务必须遵守:
{FINAL_STATE_SEMANTIC_GUARD[lang].strip()}

当前任务状态（由任务管理器维护，事实来自 auditor）:
{task_state.strip() or "(当前没有已维护任务状态。)"}

稳定任务契约:
{task_contract.strip() or "(没有单独契约；以分配计划中的任务契约段为准。)"}

分配的 {role_name} 子任务合同:
{plan_text.rstrip()}

按 round id 选择的相关 auditor 报告:
{related_auditor_reports.strip() or "(任务管理器没有引用相关报告。)"}

只完成当前子任务。把已审计状态和稳定契约视为可信语义边界；不要重复已审计工作，不要使用 suspect/violation、伪造、不可信或已删除产物。上下文不足或需要另一主任务类型时停止并报告，不要猜测或全局重规划。
"""


def build_role_auditor_prompt(
    *,
    task: str,
    plan_text: str,
    executor_output: str,
    next_step: RoleNextStep,
    task_state: str = "",
    task_contract: str = "",
    related_auditor_reports: str = "",
    max_executor_output_chars: int = 24_000,
    language: str = "en",
) -> str:
    lang = normalize_prompt_language(language)
    gui = next_step == MANAGER_NEXT_GUI
    role_name = "GUI/visual" if gui and lang == "en" else "CLI/non-GUI" if lang == "en" else "GUI/视觉" if gui else "CLI/非 GUI"
    role_instructions = GUI_AUDITOR_INSTRUCTIONS[lang] if gui else CLI_AUDITOR_INSTRUCTIONS[lang]
    if lang == "en":
        return f"""\
{role_instructions.strip()}

Original task:
{task.rstrip()}

Mandatory final-state guard:
{FINAL_STATE_SEMANTIC_GUARD[lang].strip()}

Current task state (background only; audit only this subtask):
{task_state.strip() or "(No maintained task state.)"}

Stable task contract (primary target reference, but do not assume it is correct):
{task_contract.strip() or "(No separate contract was maintained; use the Task contract section in the assigned plan.)"}

Just-finished {role_name} subtask:
{plan_text.rstrip()}

Executor natural-language output:
{_clip_preserve(executor_output.rstrip(), max_executor_output_chars)}

Related auditor reports (background, never a substitute for direct read-only audit):
{related_auditor_reports.strip() or "(The manager referenced no related auditor report.)"}

{AUDITOR_CONTRACT_BACKCHECK[lang].strip()}

Audit only whether this subtask truly completed, stayed within its dominant boundary, and remained trustworthy. Record still-trusted state, new trusted artifacts, and untrusted/deleted artifacts. End with `State update for manager:`. Never output JSON.
"""
    return f"""\
{role_instructions.strip()}

原始任务:
{task.rstrip()}

必须遵守的最终状态约束:
{FINAL_STATE_SEMANTIC_GUARD[lang].strip()}

当前任务状态（仅作背景；只审当前子任务）:
{task_state.strip() or "(当前没有已维护任务状态。)"}

稳定任务契约（主要目标依据，但不能默认它正确）:
{task_contract.strip() or "(没有单独契约；以分配计划中的任务契约段为准。)"}

刚完成的 {role_name} 子任务:
{plan_text.rstrip()}

executor 自然语言输出:
{_clip_preserve(executor_output.rstrip(), max_executor_output_chars)}

相关 auditor 报告（只作背景，不能代替当前只读审计）:
{related_auditor_reports.strip() or "(任务管理器没有引用相关报告。)"}

{AUDITOR_CONTRACT_BACKCHECK[lang].strip()}

只审计当前子任务是否真实完成、是否保持主目标边界、是否可信。写清仍可信状态、新增可信产物和不可信/已删除产物；以 `给任务管理器的状态更新:` 结束。不要输出 JSON。
"""


def build_role_auditor_format_repair_prompt(*, report_text: str, language: str = "en") -> str:
    lang = normalize_prompt_language(language)
    if lang == "en":
        return f"""\
Your previous auditor report lacks a valid three-line control header. This is formatting repair, not a new audit: use no tools and change no environment state. Re-emit the same report from its existing content only.

The first three nonempty lines must be exactly one value from each group:
Status: complete | Status: incomplete | Status: blocked
Integrity: clean | Integrity: suspect | Integrity: violation
Contract audit: aligned | Contract audit: unknown | Contract audit: needs_revision | Contract audit: invalid

Use aligned only when the report's acceptance-constraint backcheck explicitly supports it. If the conclusion cannot be determined, conservatively use:
Status: blocked
Integrity: suspect
Contract audit: unknown

Previous auditor report:
{report_text.strip() or "(empty report)"}

Output only the repaired auditor report. Do not explain the repair and do not output JSON.
"""
    return f"""\
上一份 auditor 报告缺少有效的前三行控制头。这只是格式修正，不是重新审计；不要使用工具或改变环境，只根据已有报告重发同一内容。

前三个非空行必须分别严格选择一个值:
状态: complete | 状态: incomplete | 状态: blocked
完整性: clean | 完整性: suspect | 完整性: violation
契约审计: aligned | 契约审计: unknown | 契约审计: needs_revision | 契约审计: invalid

只有报告的验收约束反查明确支持时才能使用 aligned。无法判断时保守输出:
状态: blocked
完整性: suspect
契约审计: unknown

上一份 auditor 报告:
{report_text.strip() or "(空报告)"}

只输出修正后的 auditor 报告，不解释格式修正，不输出 JSON。
"""


def build_role_final_response_prompt(
    *,
    task: str,
    rounds: list[ManagedRound],
    status: str,
    abort_reason: str,
    task_state: str,
    language: str = "en",
    max_evidence_chars: int = 6_000,
) -> str:
    lang = normalize_prompt_language(language)
    findings = format_audit_findings(rounds, max_chars=max_evidence_chars, language=lang)
    if lang == "en":
        outcome = f"Run outcome: {status}"
        if abort_reason:
            outcome += f" (ended because: {abort_reason})"
        return f"""\
{FINAL_RESPONSE_INSTRUCTIONS[lang].strip()}

Original request:
{task.rstrip()}

{outcome}

Verified state:
{task_state.strip() or "(Nothing was verified.)"}

Audit findings:
{findings or "(No audit was produced.)"}

Write the reply now. Output only the reply.
"""
    outcome = f"运行结果: {status}"
    if abort_reason:
        outcome += f"（结束原因: {abort_reason}）"
    return f"""\
{FINAL_RESPONSE_INSTRUCTIONS[lang].strip()}

原始任务:
{task.rstrip()}

{outcome}

已验证状态:
{task_state.strip() or "(没有任何已验证内容。)"}

审计结论:
{findings or "(没有产生审计结论。)"}

现在写这份回复。只输出回复本身。
"""


# Control headers and the backcheck protocol are addressed to the manager, so the
# user-facing reply only needs the prose findings underneath them.
_AUDIT_HEADER_RE = re.compile(
    r"^(?:\*\*)?\s*(?:状态|status|完整性|integrity|契约审计|contract(?:[_\s-]*audit)?)\s*[:：]",
    re.IGNORECASE,
)
_AUDIT_PROTOCOL_RE = re.compile(
    r"^(?:\*\*)?\s*(?:验收约束反查|契约结论|原题约束清单|契约覆盖检查|逐项反查|阻断约束|可能评分风险"
    r"|过窄或错误解释|建议契约修订|给任务管理器的状态更新"
    r"|acceptance[-\s]constraint\s+backcheck|contract\s+conclusion|original\s+constraint\s+inventory"
    r"|contract\s+coverage\s+check|per[-\s]constraint\s+backcheck|blocking\s+constraints"
    r"|possible\s+scoring\s+risks|over[-\s]narrow[^:：]*|recommended\s+contract\s+revision"
    r"|state\s+update\s+for\s+manager)\s*[:：]",
    re.IGNORECASE,
)


def format_audit_findings(
    rounds: list[ManagedRound],
    *,
    max_chars: int = 6_000,
    language: str = "en",
) -> str:
    """Condense each round's audit into the findings a user-facing reply needs."""
    lang = normalize_prompt_language(language)
    sections: list[str] = []
    for item in rounds:
        report = (item.auditor_report or "").strip()
        if not report:
            continue
        body: list[str] = []
        verdicts: list[str] = []
        skipping_protocol = False
        for line in report.splitlines():
            value = line.strip()
            if not value:
                continue
            if _AUDIT_HEADER_RE.match(value):
                verdict = value.split(":", 1)[-1].split("：", 1)[-1].strip().strip("*")
                if verdict:
                    verdicts.append(verdict)
                continue
            if _AUDIT_PROTOCOL_RE.match(value):
                skipping_protocol = True
                continue
            if skipping_protocol:
                continue
            body.append(value)
        heading = (
            f"Round {item.round_index}" if lang == "en" else f"第 {item.round_index} 轮"
        ) + (f" ({'/'.join(verdicts)})" if verdicts else "")
        sections.append("\n".join([heading, _clip_preserve("\n".join(body), 1200)]))
    return _clip_preserve("\n\n".join(sections), max_chars)


def parse_role_manager_next_step(text: str) -> RoleNextStep:
    for line in str(text or "").splitlines():
        normalized = line.strip().strip("*").replace(" ", "").replace("　", "").lower()
        if normalized in {"下一步:gui任务", "下一步：gui任务", "next:gui"}:
            return MANAGER_NEXT_GUI
        if normalized in {"下一步:cli任务", "下一步：cli任务", "next:cli"}:
            return MANAGER_NEXT_CLI
        if normalized in {"下一步:请示用户", "下一步：请示用户", "下一步:请示", "下一步：请示", "下一步:询问用户", "下一步：询问用户", "next:ask"}:
            return MANAGER_NEXT_ASK
        if normalized in {"下一步:完成", "下一步：完成", "next:done", "next:complete"}:
            return MANAGER_NEXT_DONE
        if normalized in {"下一步:阻塞", "下一步：阻塞", "next:blocked"}:
            return MANAGER_NEXT_BLOCKED
    return MANAGER_NEXT_INVALID


def extract_role_manager_question(plan_text: str) -> str:
    """Return the `问题:` block from a `下一步: 请示用户` plan (question for the human)."""
    lines = str(plan_text or "").splitlines()
    collecting = False
    collected: list[str] = []
    for line in lines:
        head = line.strip().strip("*").replace(" ", "").replace("　", "")
        if not collecting:
            if head.startswith(("问题:", "问题：")) or re.match(r"(?i)^question\s*:", line.strip().strip("*")):
                collecting = True
                # Split on the heading's own colon, whichever form it is; a colon
                # later in the question text must not truncate it.
                rest = re.split(r"[:：]", line, maxsplit=1)[-1]
                if rest.strip():
                    collected.append(rest.strip())
            continue
        # stop at the next known section header
        if re.match(r"(?i)^\s*(?:\*\*)?\s*(?:选项|当前任务状态|任务契约|依赖判断|下一步|任务|验收标准|相关审计报告|相关已审计状态|边界|choices|current\s+task\s+state|task\s+contract|dependency\s+assessment|next|task|acceptance\s+criteria|related\s+audit\s+reports|related\s+audited\s+state|boundaries)\s*[:：]", line):
            break
        collected.append(line.rstrip())
    return "\n".join(collected).strip()


def extract_role_manager_answer_choices(plan_text: str) -> list[str]:
    """Return quick-answer choices for a `请示用户` gate.

    Prefers an explicit ``选项:`` line (e.g. ``选项: 是 | 否``); when absent but the
    question is clearly yes/no, falls back to ``["是", "否"]``. Empty means the
    human just types a free-form answer.
    """
    for line in str(plan_text or "").splitlines():
        stripped = line.strip().strip("*")
        m = re.match(r"(?i)^\s*(?:选项|choices)\s*[:：]\s*(.+)$", stripped)
        if m:
            raw = m.group(1)
            parts = re.split(r"\s*[|、,，/]\s*", raw)
            choices = [p.strip() for p in parts if p.strip()]
            if choices:
                return choices[:6]
    question = extract_role_manager_question(plan_text)
    if "是否" in question or ("是" in question and "否" in question):
        return ["是", "否"]
    if re.search(r"(?i)\b(?:yes\s*/\s*no|whether)\b", question):
        return ["Yes", "No"]
    return []


_MANAGER_ROUTE_LINE_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*(?:下一步\s*[:：]\s*(?:GUI任务|CLI任务|请示用户|请示|询问用户|完成|阻塞)|next\s*:\s*(?:gui|cli|ask|done|complete|blocked))"
)


def extract_role_manager_plan_text(text: str) -> str:
    """Return the final state+route block from a noisy assistant transcript.

    Claude Code stream logs can contain separate thinking and final assistant
    message records. The manager contract is the final natural-language
    task-state plus route block, not wrapper transcript headings.
    """
    raw = str(text or "").strip()
    matches = list(_MANAGER_ROUTE_LINE_RE.finditer(raw))
    if not matches:
        return raw
    route_start = matches[-1].start()
    starts = [
        position
        for position in (
            raw.rfind("当前任务状态", 0, route_start),
            raw.lower().rfind("current task state", 0, route_start),
            raw.rfind("任务契约", 0, route_start),
            raw.lower().rfind("task contract", 0, route_start),
        )
        if position >= 0
    ]
    if starts:
        return raw[min(starts):].strip()
    return raw[route_start:].strip()


_TASK_STATE_HEADER_RE = re.compile(r"(?im)^\s*(?:\*\*)?\s*(?:当前任务状态|current\s+task\s+state)\s*[:：]")
_TASK_STATE_BOUNDARY_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*(?:任务契约|task\s+contract|依赖判断|dependency\s+assessment"
    r"|下一步\s*[:：]\s*(?:GUI任务|CLI任务|请示用户|请示|询问用户|完成|阻塞)"
    r"|next\s*:\s*(?:gui|cli|ask|done|complete|blocked))"
)
_TASK_CONTRACT_HEADER_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*(?:任务契约|任务语义合同|task\s+contract)\s*[:：]"
)
_TASK_CONTRACT_BOUNDARY_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*(?:依赖判断|dependency\s+assessment"
    r"|下一步\s*[:：]\s*(?:GUI任务|CLI任务|请示用户|请示|询问用户|完成|阻塞)"
    r"|next\s*:\s*(?:gui|cli|ask|done|complete|blocked))"
)
_RELATED_REPORT_SECTION_RE = re.compile(
    r"(?ims)^\s*(?:\*\*)?\s*(?:相关(?:审计报告|已审计状态)|related\s+(?:audit\s+reports|audited\s+state))\s*[:：]\s*(.*?)(?=^\s*(?:\*\*)?\s*(?:边界|任务|验收标准|下一步|boundaries|task|acceptance\s+criteria|next)\s*[:：]|\Z)"
)
_ROUND_REF_RE = re.compile(
    r"(?i)\bround_(\d+)\b"
)


def extract_role_task_state(text: str, *, fallback: str = "") -> str:
    raw = str(text or "").strip()
    match = _TASK_STATE_HEADER_RE.search(raw)
    if not match:
        return fallback.strip()
    boundary = _TASK_STATE_BOUNDARY_RE.search(raw, match.end())
    end = boundary.start() if boundary else len(raw)
    state = raw[match.start() : end].strip()
    return state or fallback.strip()


def extract_role_task_contract(text: str, *, fallback: str = "") -> str:
    raw = str(text or "").strip()
    match = _TASK_CONTRACT_HEADER_RE.search(raw)
    if not match:
        return fallback.strip()
    boundary = _TASK_CONTRACT_BOUNDARY_RE.search(raw, match.end())
    end = boundary.start() if boundary else len(raw)
    contract = raw[match.start() : end].strip()
    return contract or fallback.strip()


def extract_related_report_refs(text: str) -> list[str]:
    raw = str(text or "")
    sections = [m.group(1) for m in _RELATED_REPORT_SECTION_RE.finditer(raw)]
    search_text = "\n".join(sections) if sections else raw
    refs: list[str] = []
    seen: set[int] = set()
    for match in _ROUND_REF_RE.finditer(search_text):
        value = next((item for item in match.groups() if item), "")
        if not value:
            continue
        number = int(value)
        if number <= 0 or number in seen:
            continue
        seen.add(number)
        refs.append(f"round_{number:03d}")
    return refs


def format_related_auditor_reports(
    rounds: list[ManagedRound],
    refs: list[str],
    *,
    max_chars: int = 60_000,
    language: str = "en",
) -> str:
    selected: list[ManagedRound] = []
    ref_numbers = {_round_ref_to_index(item) for item in refs}
    ref_numbers.discard(None)
    for item in rounds:
        if item.round_index in ref_numbers and item.auditor_report.strip():
            selected.append(item)
    return format_verified_intermediate_context(
        selected, max_chars=max_chars, language=language
    )


def format_harness_feedback_context(rounds: list[ManagedRound], *, max_chars: int = 12_000) -> str:
    sections: list[str] = []
    for item in rounds:
        feedback = (item.harness_feedback or "").strip()
        if not feedback:
            continue
        sections.append(
            "\n".join(
                (
                    f"--- Round {item.round_index} harness feedback ---",
                    feedback,
                )
            )
        )
    return _clip_preserve("\n\n".join(sections), max_chars)


def format_verified_intermediate_context(
    rounds: list[ManagedRound],
    *,
    max_chars: int = 30_000,
    language: str = "en",
) -> str:
    lang = normalize_prompt_language(language)
    sections: list[str] = []
    for item in rounds:
        report = (item.auditor_report or "").strip()
        if not report:
            continue
        subtask = (item.plan_text or "").strip()
        sections.append(
            "\n".join(
                part
                for part in (
                    f"--- Round {item.round_index} auditor report ---",
                    f"round_id: round_{item.round_index:03d}",
                    "Assigned subtask:" if lang == "en" else "对应子任务:",
                    _clip_preserve(subtask, 1600) if subtask else "(无)",
                    "Original auditor report:" if lang == "en" else "auditor 报告原文:",
                    _clip_preserve(report, 5000),
                )
                if part is not None
            )
        )
    return _clip_preserve("\n\n".join(sections), max_chars)


def _round_ref_to_index(ref: str) -> int | None:
    match = _ROUND_REF_RE.search(str(ref or ""))
    if not match:
        return None
    value = next((item for item in match.groups() if item), "")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def format_management_history(
    rounds: list[ManagedRound],
    *,
    include_empty: bool = False,
    max_chars: int = 36_000,
) -> str:
    sections: list[str] = []
    for item in rounds:
        if not include_empty and not (item.plan_text or item.executor_output or item.auditor_report or item.harness_feedback):
            continue
        sections.append(
            "\n".join(
                part
                for part in (
                    f"--- Round {item.round_index}: {item.next_step} ---",
                    "稳定任务契约 / Stable task contract:",
                    _clip_preserve(item.task_contract.strip(), 3000) if item.task_contract else "(无 / none)",
                    "任务管理器子任务:",
                    _clip_preserve(item.plan_text.strip(), 5000),
                    "executor agent 输出:",
                    _clip_preserve(item.executor_output.strip(), 5000) if item.executor_output else "(无)",
                    "auditor 自然语言审计报告:",
                    _clip_preserve(item.auditor_report.strip(), 6000) if item.auditor_report else "(无)",
                    "harness 任务管理反馈:",
                    _clip_preserve(item.harness_feedback.strip(), 2000) if item.harness_feedback else "(无)",
                )
                if part is not None
            )
        )
    return _clip_preserve("\n\n".join(sections), max_chars)


def _clip_preserve(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head_chars = max(1, int(max_chars * 0.65))
    tail_chars = max(1, max_chars - head_chars)
    return (
        text[:head_chars].rstrip()
        + f"\n\n...[truncated {len(text) - max_chars} chars; kept head and tail]...\n\n"
        + text[-tail_chars:].lstrip()
    )
