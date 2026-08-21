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
from .types import ExecutorRoutingConfig, ManagedRound, PromptLanguage, RoleNextStep

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
    round_budget: int | None = None,
    executor_tiers: dict[str, dict[str, str]] | None = None,
    executor_routing: ExecutorRoutingConfig | None = None,
    language: str = "en",
    max_history_chars: int = 36_000,
) -> str:
    lang = normalize_prompt_language(language)
    configured_budget = max(round_index, int(round_budget or round_index))
    remaining_rounds = max(1, configured_budget - round_index + 1)
    auditor_reports = format_verified_intermediate_context(
        rounds, max_chars=max_history_chars, language=lang
    )
    harness_feedback = format_harness_feedback_context(rounds, max_chars=max_history_chars)
    routing_policy = _format_executor_routing_policy(
        executor_tiers=executor_tiers,
        executor_routing=executor_routing,
        language=lang,
    )
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

Round budget:
- Current management round: {round_index}
- Configured round limit: {configured_budget}
- Rounds remaining, including this one: {remaining_rounds}
- If only one round remains, do not schedule a prerequisite-only subtask that deliberately postpones core requirements. Route the most complete executable subtask possible, or ask/block when completion is impossible.

{routing_policy}

Output only the next management result.
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

轮次预算:
- 当前任务管理轮次: {round_index}
- 配置的轮次上限: {configured_budget}
- 包含本轮在内的剩余轮次: {remaining_rounds}
- 如果只剩一轮，不得安排一个明确推迟核心要求的纯前置子任务；应路由当前可完成的最完整子任务，无法完成时使用 ask 或 blocked。

{routing_policy}

只输出下一步任务管理结果。
"""


def _format_executor_routing_policy(
    *,
    executor_tiers: dict[str, dict[str, str]] | None,
    executor_routing: ExecutorRoutingConfig | None,
    language: PromptLanguage,
) -> str:
    if executor_routing is None:
        return ""
    tier_names = ", ".join(sorted((executor_tiers or {}).keys()))
    if language == "en":
        return f"""\
Executor routing policy:
- Allowed executor tiers: {tier_names or "(none configured)"}
- Default tier when `Executor tier:` is omitted: {executor_routing.default_tier}
- Automatic escalation: after {executor_routing.escalate_after_failures} successfully parsed `Status: incomplete` or `Status: blocked` verdicts for the same subtask, the harness may force tier {executor_routing.escalation_tier}. Provider/runtime failures, timeouts, and format-repair failures do not count.
- GUI/CLI output schema: immediately after the route line and before `Task:`, you may add `Executor tier: <name>` to select the initial tier. Omitting it uses the default; naming {executor_routing.escalation_tier} uses it immediately.
- Output only an allowed tier name. Never add `Executor tier:` to ask/done/blocked routes."""
    return f"""\
执行器路由策略:
- 允许的执行器层级: {tier_names or "（未配置）"}
- 省略 `执行器层级:` 时的默认层级: {executor_routing.default_tier}
- 自动升级: 同一子任务出现 {executor_routing.escalate_after_failures} 次成功解析的 `状态: incomplete` 或 `状态: blocked` 结论后，harness 可以强制使用层级 {executor_routing.escalation_tier}。provider/runtime 失败、超时和格式修复失败不计数。
- GUI/CLI 输出格式: 路由行后、`任务:` 前可以紧接 `执行器层级: <名称>` 来选择初始层级；省略时使用默认层级，填写 {executor_routing.escalation_tier} 会立即使用该层级。
- 只能输出允许的层级名称。请示用户/完成/阻塞路由绝不能添加 `执行器层级:`。"""


def build_role_executor_prompt(
    *,
    task: str,
    plan_text: str,
    next_step: RoleNextStep,
    task_state: str = "",
    task_contract: str = "",
    related_auditor_reports: str = "",
    workspace_path: str = "",
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

Real task state and deliverables:
- Workspace root: {workspace_path.strip() or "(Use the executor environment's current workspace.)"}
- Durable files belong in that workspace (or in the explicitly requested target application), not in the harness run-record directory.
- Report exact paths and observable target-application state so the Auditor can independently verify them. Harness prompts, trajectories, and role output logs are execution records, not proof that the task succeeded.

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

真实任务状态和交付物:
- Workspace 根目录: {workspace_path.strip() or "(使用 executor 环境的当前 workspace。)"}
- 持久文件应写入该 Workspace（或任务明确指定的目标应用），不要把 harness 运行记录目录当成交付目录。
- 报告准确路径和可观察的目标应用状态，供 Auditor 独立核验。prompt、trajectory 和角色输出日志只是运行记录，不是任务完成证明。

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
    workspace_path: str = "",
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

Independent evidence boundary:
- Real workspace root: {workspace_path.strip() or "(Use the auditor environment's current workspace.)"}
- Independently inspect claimed files, commands/tests/logs/services, and the current target GUI/application when relevant. Executor text is only a claim.
- Harness prompts, trajectories, role outputs, and prior reports are run records, not task deliverables or standalone completion evidence. Do not require every subtask to create a file when its consumed result is legitimately application state, a user-facing response, or independently observable external state.
- If the Executor deliberately saved a screenshot or visual artifact in the real task environment, inspect that artifact and its current source state. Private Dashboard trajectory images are operator run records and are not injected as audit evidence.

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

独立证据边界:
- 真实 Workspace 根目录: {workspace_path.strip() or "(使用 auditor 环境的当前 workspace。)"}
- 独立检查声明的文件、命令/测试/日志/服务，以及相关时的当前 GUI/目标应用状态；Executor 文本只是一项声明。
- prompt、trajectory、角色输出和历史报告只是运行记录，不是任务交付物或可单独成立的完成证据。若子任务的真实结果本来就是应用状态、面向用户的回复或可独立观察的外部状态，不要机械要求每个子任务必须创建文件。
- 如果 Executor 有意在真实任务环境中保存了截图或视觉产物，应检查该产物及其当前来源状态。Dashboard 的私有 trajectory 图片只是给操作者查看的运行记录，不会作为审计证据注入。

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
    operator_instructions: str = "",
    language: str = "en",
    max_evidence_chars: int = 6_000,
    max_deliverable_chars: int = 24_000,
) -> str:
    lang = normalize_prompt_language(language)
    findings = format_audit_findings(rounds, max_chars=max_evidence_chars, language=lang)
    # A successful completion is grounded in the latest clean/complete/aligned
    # auditor report. Give the response writer the corresponding executor output
    # as well as the condensed audit: the audit often confirms links, figures, or
    # other required details without repeating the whole user-facing deliverable.
    verified_deliverable = ""
    if status == "complete":
        verified_deliverable = next(
            (
                item.executor_output.strip()
                for item in reversed(rounds)
                if item.executor_output.strip() and item.auditor_report.strip()
            ),
            "",
        )[:max_deliverable_chars]
    if lang == "en":
        outcome = f"Run outcome: {status}"
        if abort_reason:
            outcome += f" (ended because: {abort_reason})"
        return f"""\
{FINAL_RESPONSE_INSTRUCTIONS[lang].strip()}

Original request:
{task.rstrip()}

Authoritative operator follow-up instructions:
{operator_instructions.strip() or "(None.)"}

{outcome}

Verified state:
{task_state.strip() or "(Nothing was verified.)"}

Audit findings:
{findings or "(No audit was produced.)"}

Verified deliverable from the accepted executor:
{verified_deliverable or "(No standalone deliverable was produced.)"}

Write the reply now. Output only the reply.
"""
    outcome = f"运行结果: {status}"
    if abort_reason:
        outcome += f"（结束原因: {abort_reason}）"
    return f"""\
{FINAL_RESPONSE_INSTRUCTIONS[lang].strip()}

原始任务:
{task.rstrip()}

操作员后续补充的权威指令:
{operator_instructions.strip() or "(无。)"}

{outcome}

已验证状态:
{task_state.strip() or "(没有任何已验证内容。)"}

审计结论:
{findings or "(没有产生审计结论。)"}

已通过验收的执行器交付正文:
{verified_deliverable or "(没有独立的交付正文。)"}

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


def _parse_role_manager_route_line(line: str) -> RoleNextStep:
    normalized = str(line or "").strip().strip("*").replace(" ", "").replace("　", "").lower()
    # Models commonly append a short rationale after the required route,
    # e.g. `Next: done — all constraints passed`. Treat only an explicitly
    # delimited suffix as commentary so prose such as `Next: done later`
    # remains invalid.
    normalized = re.split(r"(?:—|–|--|//|#|[（(])", normalized, maxsplit=1)[0]
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


def parse_role_manager_next_step(text: str) -> RoleNextStep:
    """Return the same structural route authority used by tier/task parsing."""

    lines = str(text or "").splitlines()
    authority = _last_manager_route(lines)
    if authority is not None:
        return authority[1]
    return MANAGER_NEXT_INVALID


_EXECUTOR_TIER_HEADER_RE = re.compile(
    r"(?i)^(?:\*\*)?\s*(?:executor\s+tier|执行器层级)\s*[:：]\s*(?:\*\*)?\s*(.*)$"
)
_MANAGER_TASK_HEADER_RE = re.compile(
    r"(?i)^(?:\*\*)?\s*(?:task|任务)\s*[:：]\s*(?:\*\*)?\s*(.*)$"
)
_MANAGER_SUBTASK_BOUNDARY_RE = re.compile(
    r"(?i)^(?:\*\*)?\s*(?:acceptance\s+criteria|related\s+audit\s+reports"
    r"|related\s+audited\s+state|boundaries|验收标准|相关审计报告|相关已审计状态|边界)\s*[:：]"
)
_MANAGER_BOUNDARIES_HEADER_RE = re.compile(
    r"(?i)^(?:\*\*)?\s*(?:boundaries|边界)\s*[:：]"
)
_MANAGER_BLOCK_START_RE = re.compile(
    r"(?i)^(?:\*\*)?\s*(?:current\s+task\s+state|当前任务状态)\s*[:：]"
)
_MANAGER_TASK_CONTRACT_HEADER_RE = re.compile(
    r"(?i)^(?:\*\*)?\s*(?:task\s+contract|任务契约|任务语义合同)\s*[:：]"
)
_MANAGER_DEPENDENCY_HEADER_RE = re.compile(
    r"(?i)^(?:\*\*)?\s*(?:dependency\s+assessment|依赖判断)\s*[:：]"
)
_MANAGER_BLOCK_SEPARATOR_RE = re.compile(r"^\s*(?:-{3,}|={3,})\s*$")


def extract_role_manager_executor_tier(plan_text: str) -> str | None:
    """Return the optional tier header from the final executable route block.

    ``None`` means the manager omitted the header. An explicitly empty header
    remains ``""`` so the runtime can reject it instead of silently selecting
    a default tier.
    """

    lines = str(plan_text or "").splitlines()
    route_index = _last_executable_route_index(lines)
    if route_index is None:
        return None
    for line in lines[route_index + 1 :]:
        if not line.strip():
            continue
        match = _EXECUTOR_TIER_HEADER_RE.match(line.strip())
        return match.group(1).strip() if match else None
    return None


def extract_role_manager_task(plan_text: str) -> str:
    """Return the Task body from the final executable manager route block."""

    lines = str(plan_text or "").splitlines()
    route_index = _last_executable_route_index(lines)
    if route_index is None:
        return ""

    cursor = route_index + 1
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    if cursor < len(lines) and _EXECUTOR_TIER_HEADER_RE.match(lines[cursor].strip()):
        cursor += 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
    if cursor >= len(lines):
        return ""
    task_match = _MANAGER_TASK_HEADER_RE.match(lines[cursor].strip())
    if task_match is None:
        return ""

    collected: list[str] = []
    inline = task_match.group(1).strip()
    if inline:
        collected.append(inline)
    for line in lines[cursor + 1 :]:
        if _MANAGER_SUBTASK_BOUNDARY_RE.match(line.strip()):
            break
        collected.append(line.rstrip())
    return "\n".join(collected).strip()


def _last_executable_route_index(lines: list[str]) -> int | None:
    route = _last_manager_route(lines)
    if route is None or route[1] not in {MANAGER_NEXT_GUI, MANAGER_NEXT_CLI}:
        return None
    return route[0]


def _last_manager_route(
    lines: list[str],
) -> tuple[int, RoleNextStep, int] | None:
    """Return the final authoritative route and its manager-block start.

    Once an executable route is accepted, every following subtask section is
    payload. Route-shaped literals inside Task, acceptance criteria, related
    evidence, or boundaries cannot silently replace the controlling route.
    """

    last_route: tuple[int, RoleNextStep, int] | None = None
    candidate_block_start: int | None = None
    candidate_stage = 0  # 1=current state, 2=task contract, 3=dependency
    candidate_short = False
    payload_locked = False
    boundaries_seen = False
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if payload_locked:
            if _MANAGER_BOUNDARIES_HEADER_RE.match(stripped):
                boundaries_seen = True
            if boundaries_seen and _MANAGER_BLOCK_SEPARATOR_RE.match(stripped):
                payload_locked = False
                boundaries_seen = False
                candidate_block_start = None
                candidate_stage = 0
                candidate_short = False
                index += 1
                continue
            if _MANAGER_BLOCK_START_RE.match(stripped):
                candidate_block_start = index
                candidate_stage = 1
                candidate_short = False
                index += 1
                continue
            if candidate_stage == 1 and _MANAGER_TASK_CONTRACT_HEADER_RE.match(stripped):
                candidate_stage = 2
                index += 1
                continue
            if candidate_stage == 2 and _MANAGER_DEPENDENCY_HEADER_RE.match(stripped):
                candidate_stage = 3
                index += 1
                continue
            route = _parse_role_manager_route_line(lines[index])
            if candidate_stage != 3 or route == MANAGER_NEXT_INVALID:
                index += 1
                continue
            if route in {MANAGER_NEXT_GUI, MANAGER_NEXT_CLI}:
                valid_start, _ = _subtask_protocol_start(lines, index)
                if not valid_start:
                    index += 1
                    continue
            last_route = (
                index,
                route,
                candidate_block_start if candidate_block_start is not None else index,
            )
            candidate_block_start = None
            candidate_stage = 0
            candidate_short = False
            boundaries_seen = False
            index += 1
            continue

        if _MANAGER_BLOCK_START_RE.match(stripped):
            candidate_block_start = index
            candidate_stage = 1
            candidate_short = False
            index += 1
            continue
        if candidate_stage == 0 and _MANAGER_TASK_CONTRACT_HEADER_RE.match(stripped):
            # Backward-compatible initial block: old transcripts can begin at
            # Task contract, but this short form never reopens locked payload.
            candidate_block_start = index
            candidate_stage = 2
            candidate_short = True
            index += 1
            continue
        if candidate_stage == 1 and _MANAGER_TASK_CONTRACT_HEADER_RE.match(stripped):
            candidate_stage = 2
            index += 1
            continue
        if candidate_stage == 2 and _MANAGER_DEPENDENCY_HEADER_RE.match(stripped):
            candidate_stage = 3
            index += 1
            continue
        route = _parse_role_manager_route_line(lines[index])
        if route == MANAGER_NEXT_INVALID:
            index += 1
            continue
        if candidate_stage not in {0, 3} and not (
            candidate_short and candidate_stage == 2
        ):
            index += 1
            continue
        if route in {MANAGER_NEXT_GUI, MANAGER_NEXT_CLI}:
            valid_start, _ = _subtask_protocol_start(lines, index)
            if not valid_start:
                index += 1
                continue
            last_route = (
                index,
                route,
                candidate_block_start if candidate_block_start is not None else index,
            )
            payload_locked = True
            boundaries_seen = False
            candidate_block_start = None
            candidate_stage = 0
            candidate_short = False
            index += 1
            continue
        last_route = (
            index,
            route,
            candidate_block_start if candidate_block_start is not None else index,
        )
        payload_locked = True
        boundaries_seen = False
        candidate_block_start = None
        candidate_stage = 0
        candidate_short = False
        index += 1
    return last_route


def _subtask_protocol_start(
    lines: list[str], route_index: int
) -> tuple[bool, int | None]:
    cursor = route_index + 1
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    if cursor >= len(lines):
        return False, None
    first_protocol_line = lines[cursor].strip()
    if _MANAGER_BLOCK_START_RE.match(first_protocol_line):
        # Legacy short plans put the maintained state after the route and may
        # omit Task entirely. This form is authoritative only when the state
        # header immediately follows the top-level executable route.
        return True, None
    if _MANAGER_TASK_HEADER_RE.match(first_protocol_line):
        return True, cursor
    if not _EXECUTOR_TIER_HEADER_RE.match(first_protocol_line):
        return False, None
    cursor += 1
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    task_index = (
        cursor
        if cursor < len(lines) and _MANAGER_TASK_HEADER_RE.match(lines[cursor].strip())
        else None
    )
    return True, task_index


def extract_role_manager_question(plan_text: str) -> str:
    """Return the `问题:` block from a `下一步: 请示用户` plan (question for the human)."""
    lines = str(plan_text or "").splitlines()
    authority = _last_manager_route(lines)
    if authority is None or authority[1] != MANAGER_NEXT_ASK:
        return ""
    collecting = False
    collected: list[str] = []
    for line in lines[authority[0] + 1 :]:
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
    lines = str(plan_text or "").splitlines()
    authority = _last_manager_route(lines)
    if authority is None or authority[1] != MANAGER_NEXT_ASK:
        return []
    for line in lines[authority[0] + 1 :]:
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


def extract_role_manager_plan_text(text: str) -> str:
    """Return the final state+route block from a noisy assistant transcript.

    Claude Code stream logs can contain separate thinking and final assistant
    message records. The manager contract is the final natural-language
    task-state plus route block, not wrapper transcript headings.
    """
    raw = str(text or "").strip()
    lines = raw.splitlines()
    authority = _last_manager_route(lines)
    if authority is None:
        return raw
    return "\n".join(lines[authority[2] :]).strip()


_RELATED_REPORT_SECTION_RE = re.compile(
    r"(?ims)^\s*(?:\*\*)?\s*(?:相关(?:审计报告|已审计状态)|related\s+(?:audit\s+reports|audited\s+state))\s*[:：]\s*(.*?)(?=^\s*(?:\*\*)?\s*(?:边界|任务|验收标准|下一步|boundaries|task|acceptance\s+criteria|next)\s*[:：]|\Z)"
)
_ROUND_REF_RE = re.compile(
    r"(?i)\bround_(\d+)\b"
)


def extract_role_task_state(text: str, *, fallback: str = "") -> str:
    lines = str(text or "").splitlines()
    authority = _last_manager_route(lines)
    if authority is None:
        return fallback.strip()
    state_index, contract_index, _ = _manager_protocol_section_indices(
        lines, authority
    )
    if state_index is None:
        return fallback.strip()
    end = (
        contract_index
        if contract_index is not None
        else authority[0]
        if state_index < authority[0]
        else len(lines)
    )
    state = "\n".join(lines[state_index:end]).strip()
    return state or fallback.strip()


def extract_role_task_contract(text: str, *, fallback: str = "") -> str:
    lines = str(text or "").splitlines()
    authority = _last_manager_route(lines)
    if authority is None:
        return fallback.strip()
    _, contract_index, dependency_index = _manager_protocol_section_indices(
        lines, authority
    )
    if contract_index is None:
        return fallback.strip()
    end = (
        dependency_index
        if dependency_index is not None
        else authority[0]
        if contract_index < authority[0]
        else len(lines)
    )
    contract = "\n".join(lines[contract_index:end]).strip()
    return contract or fallback.strip()


def extract_related_report_refs(text: str) -> list[str]:
    lines = str(text or "").splitlines()
    authority = _last_manager_route(lines)
    if authority is None or authority[1] not in {MANAGER_NEXT_GUI, MANAGER_NEXT_CLI}:
        return []
    raw = "\n".join(lines[authority[0] + 1 :])
    sections = [m.group(1) for m in _RELATED_REPORT_SECTION_RE.finditer(raw)]
    if not sections:
        return []
    search_text = "\n".join(sections)
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


def _manager_protocol_section_indices(
    lines: list[str], authority: tuple[int, RoleNextStep, int]
) -> tuple[int | None, int | None, int | None]:
    """Locate structural state/contract/dependency headers for one authority."""

    state_index: int | None = None
    contract_index: int | None = None
    dependency_index: int | None = None
    for index in range(authority[2], authority[0]):
        stripped = lines[index].strip()
        if state_index is None and _MANAGER_BLOCK_START_RE.match(stripped):
            state_index = index
            continue
        if contract_index is None and _MANAGER_TASK_CONTRACT_HEADER_RE.match(stripped):
            contract_index = index
            continue
        if (
            contract_index is not None
            and dependency_index is None
            and _MANAGER_DEPENDENCY_HEADER_RE.match(stripped)
        ):
            dependency_index = index
    if state_index is None:
        cursor = authority[0] + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        if cursor < len(lines) and _MANAGER_BLOCK_START_RE.match(
            lines[cursor].strip()
        ):
            state_index = cursor
            for index in range(cursor + 1, len(lines)):
                stripped = lines[index].strip()
                if contract_index is None and _MANAGER_TASK_CONTRACT_HEADER_RE.match(
                    stripped
                ):
                    contract_index = index
                    continue
                if (
                    contract_index is not None
                    and dependency_index is None
                    and _MANAGER_DEPENDENCY_HEADER_RE.match(stripped)
                ):
                    dependency_index = index
    return state_index, contract_index, dependency_index


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
