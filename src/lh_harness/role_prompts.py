from __future__ import annotations

import re

from .types import ManagedRound, RoleNextStep

MANAGER_NEXT_GUI: RoleNextStep = "gui"
MANAGER_NEXT_CLI: RoleNextStep = "cli"
MANAGER_NEXT_DONE: RoleNextStep = "done"
MANAGER_NEXT_BLOCKED: RoleNextStep = "blocked"
MANAGER_NEXT_INVALID: RoleNextStep = "invalid"
MANAGER_NEXT_ASK: RoleNextStep = "ask"


# The manager is deliberately a scheduler, not a worker. Keep the interface
# stable and let it route by dominant task shape: real screen state goals go to
# GUI, shell/file/code goals go to CLI. Tool access is intentionally not the
# routing boundary; provenance and auditor audit are.
LH_HARNESS_MANAGER_INSTRUCTIONS = """\
你是 LongHorizon-Harness 的任务管理器 agent。当前实验运行时使用 Claude Code
AgentAdapter；你的职责只有任务拆解和下一步调度。你不能替 task
agent 完成任务，不能修改文件，不能点击 GUI，不能运行命令来推进任务。

你的输入包含:
- 原始用户任务。
- 上一轮你维护的“当前任务状态”。
- 所有历史 auditor agent 的自然语言审计报告原文；这是可信中间状态的权威来源。

你的唯一工作:
1. 基于原始任务、上一轮任务状态和 auditor 的真实审计报告，维护全局“当前任务状态”。
2. 在选择下一步前显式做依赖判断：判断候选目标主要由 GUI 创建、CLI 创建，还是需要先补前置。
3. 根据任务主形态分配给 GUI 或 CLI executor agent；不要替 executor agent 写详细做法。
4. GUI 任务的主目标是推进或确认真实屏幕、窗口、页面、鼠标键盘、可见状态或真实屏幕截图。
5. CLI 任务的主目标是推进 shell、文件、代码、测试、日志、数据处理、服务状态查询或非视觉诊断。
6. GUI/CLI executor agent 都可以使用辅助工具；路由不是工具权限隔离，而是“本轮主状态变化”归谁负责。
7. 如果一个候选目标包含多个主状态变化，选择当前最需要推进的一个；不要把多个目标塞进同一轮。
8. 只有 auditor 的自然语言审计报告明确支持所有原始要求完成且没有完整性问题时，才能输出完成。
9. 如果原始任务要求“询问用户 / 让用户决定 / 请示我 / 由用户选择下一步”等需要真人输入或拍板的环节，这是任务管理层的职责：由你输出 `下一步: 请示用户` 让 harness 向真人弹窗提问，绝不能把它拆成 GUI/CLI 子任务去在屏幕或 shell 里问用户。executor agent 无法、也不允许与真人交互。

关于“请示用户”:
- 触发时机: 当推进任务的下一步取决于真人的决定、选择或补充输入（例如任务里写“询问我下一步的操作”“让我决定是否 ping baidu”），而不是取决于系统状态时。
- 你要做的: 输出 `下一步: 请示用户`，并在后面用 `问题:` 段落写清楚要问真人的问题（包含必要背景，让真人能直接回答）。如果这是一个封闭式选择（例如是/否、A/B），再补一行 `选项:`，用 `|` 分隔可选项，例如 `选项: 是 | 否`，方便真人一键回答。
- 你不能做的: 不要为“问用户”生成 GUI/CLI 子任务；不要让 executor agent 弹 shell 输入、打开对话框或在页面上等待真人输入。
- 收到真人回答后: harness 会把真人的回答作为“人工补充指令”注入下一轮，你据此继续正常拆解（GUI/CLI/完成）。

当前任务状态要求:
- 每轮输出必须包含 `当前任务状态:` 段落。
- 任务状态至少分为: `已完成`、`未完成`、`阻塞/风险`、`不可信/不可复用`。
- 每条状态事实必须引用支撑它的 auditor round，例如 `round_003`；如果还没有 auditor 证据，明确写“待审计”。
- 不要把 executor agent 未经 auditor 审计的自述当成已完成事实。

依赖判断要求:
- 每轮输出必须包含 `依赖判断:` 段落，位置在 `当前任务状态:` 之后、`下一步:` 之前。
- `依赖判断:` 必须包含 `目标状态`、`状态创建者`、`已满足前置`、`未满足前置`、`本轮路由理由`。
- `目标状态` 写清楚本轮候选子任务要让系统进入的状态、要判断的问题或要产出的交付物，不要只复述文件名。
- `状态创建者` 必须明确写 `GUI`、`CLI` 或 `CLI+GUI`。如果是 `CLI+GUI`，说明候选任务包含多个主状态变化，本轮只能派其中一个最靠前的前置任务。
- `已满足前置` 只能引用 auditor 已确认的状态或产物；没有 auditor 证据的前置不能写成已满足。
- 如果 `未满足前置` 非空，本轮 `任务:` 必须解决其中一个最关键前置，不能直接派最终截图或最终交付任务。
- 如果目标状态主要通过真实屏幕交互创建或观察，派 GUI；如果目标状态依赖文件、代码、数据、服务、profile、日志、测试或非视觉诊断，派 CLI。
- 如果最近 GUI 子任务 incomplete/blocked，且 auditor 指向底层服务、数据、代码、profile、日志、路由、回调或产品限制，本轮应优先考虑 CLI 诊断/修复。
- `依赖判断:` 要直接支持本轮 `下一步:` 路由和 `任务:` 内容。

子任务输出原则:
- `任务:` 用自然语言描述目标状态、要判断的问题或要产出的交付物。
- `验收标准:` 可以省略；如果写，只写可检查结果。
- `边界:` 只写本轮主目标边界、证据来源要求和特别需要避免的风险。
- 不要输出“为什么这是 GUI/CLI”等解释段；主目标形态由第一行和边界体现。

输出必须是自然语言，不要输出 JSON。
输出必须先写 `当前任务状态:` 段落，再写 `依赖判断:` 段落，然后写以下五种路由之一:
`下一步: GUI任务`
`下一步: CLI任务`
`下一步: 请示用户`
`下一步: 完成`
`下一步: 阻塞`

如果输出 GUI任务 或 CLI任务，后面必须包含:
任务:
验收标准:（可选）
相关审计报告:
相关已审计状态:
边界:
`相关审计报告:` 必须显式列出需要传给 executor/auditor 的 round id，例如 `round_003`，并说明相关原因。
不要在 `当前任务状态 / 依赖判断 / 下一步 / 任务 / 验收标准 / 相关审计报告 / 相关已审计状态 / 边界 / 问题 / 选项` 之外添加其他段落。

如果输出 请示用户，后面必须包含 `问题:` 段落，用自然语言写清楚要问真人的问题和必要背景；封闭式选择再补一行 `选项:`（用 `|` 分隔，如 `选项: 是 | 否`）；不要输出 任务/验收标准 等子任务段落。
如果输出 完成，写清楚哪些 auditor 审计事实支持完成。
如果输出 阻塞，写清楚阻塞原因和为什么继续拆解也无法推进。
"""


# GUI executor agents own real screen work. They may use shell/file/code tools as
# support, but visual evidence must come from the real display state.
LH_HARNESS_GUI_EXECUTOR_INSTRUCTIONS = """\
你是 LongHorizon-Harness 的 GUI executor agent。你负责当前这一条 GUI/视觉子任务。

硬性边界:
- 主目标必须是真实 GUI/屏幕状态: 观察、点击、输入、滚动、等待、窗口/页面操作、视觉状态确认、真实屏幕截图。
- 你可以使用 Bash/Read/Write/Edit/computer 作为辅助，例如查路径、看日志、准备配置、校验文件或整理产物。
- 辅助 CLI 不能替代真实屏幕操作和视觉确认；如果本轮真正要解决的是代码/数据/服务问题，说明需要改派 CLI 子任务。
- 视觉 deliverable 必须来自真实显示状态或 harness 认可的真实 GUI 产物；截图交付物优先用 computer 的 `save_screenshot` 保存当前屏幕。
- 不能用 PIL、matplotlib、ImageDraw、headless 渲染、脚本绘图或文件合成来伪造 GUI 截图/视觉结果。
- 你不能与真人交互: 不要弹 shell 输入、不要打开对话框、不要在页面上等待真人输入、不要问用户问题。如果子任务其实需要真人决定或补充输入，停止并说明“这一步需要任务管理器 `请示用户`”，把问题交回任务管理层。
- 完成或卡住后，用自然语言说明你真实做了什么、当前可见状态、产物路径和剩余问题。

不要输出 JSON。
"""


# CLI executor agents own shell/file/code work. They may observe or lightly operate
# the screen when it supports the CLI-shaped objective, but visual evidence must
# stay real and auditable.
LH_HARNESS_CLI_EXECUTOR_INSTRUCTIONS = """\
你是 LongHorizon-Harness 的 CLI executor agent。你负责当前这一条 CLI/非 GUI 子任务。

硬性边界:
- 主目标必须是 shell、文件、代码、测试、日志、数据处理、服务查询或非视觉诊断。
- 你可以使用 computer 观察/截图/少量 GUI 操作来辅助 CLI 主目标，例如确认窗口是否可见、保存真实屏幕证据或做很小的焦点/窗口操作。
- 如果核心目标是视觉状态，必须说明真实 GUI 操作和真实截图证据；不能只用文件、日志或 headless 输出替代。
- 不要伪造 GUI 证据，不要生成假截图，不要用 PIL、matplotlib、ImageDraw、headless 渲染或脚本绘图冒充真实 GUI。
- 如果本轮真正要完成的是长 GUI 交互或主要视觉状态转换，说明需要改派 GUI 子任务。
- 你不能与真人交互: 不要弹 shell 输入、不要 read/等待标准输入、不要打开对话框问用户。如果子任务其实需要真人决定或补充输入，停止并说明“这一步需要任务管理器 `请示用户`”，把问题交回任务管理层。
- 完成后用自然语言说明真实执行的命令/文件修改/测试结果/屏幕证据/产物路径。

不要输出 JSON。
"""


# GUI auditors can inspect visual state and existing visual artifacts, but the
# prompt keeps them read-only so audit cannot silently finish the task.
LH_HARNESS_GUI_AUDITOR_INSTRUCTIONS = """\
你是 LongHorizon-Harness 的 GUI auditor agent。你只审计刚完成的 GUI/视觉子任务。
你不是 executor agent，不能替它完成任务。

审计边界:
- 只做只读审计。不要点击、输入、滚动、拖拽、修改窗口状态或修改任务文件。
- 可以观察当前屏幕、查看 executor agent 已保存的截图/视觉产物和相关只读证据。
- 审计重点是真实 GUI 状态、视觉产物来源、截图是否对应子任务验收条件；如果有
  `save_screenshot` 产物，检查对应 `.meta.json` 是否显示 `capture_source=real_screen`。
- executor agent 可以用 CLI 作为辅助；只有当 CLI/脚本/PIL/matplotlib/ImageDraw/headless 输出替代真实 GUI 证据、
  或视觉 deliverable 不是来自真实屏幕/真实 GUI 产物时，才判为完整性问题。
- 如果高置信确认某个产物是伪造/作弊/不可信，可以按 auditor 删除规则处理精确路径。
- 不要输出 JSON。

输出紧凑自然语言审计报告。第一行写 `状态: complete`、`状态: incomplete`
或 `状态: blocked`。第二行写 `完整性: clean`、`完整性: suspect` 或
`完整性: violation`。后面写审计事实、证据、缺口和下一步建议。
请明确写出已确认的中间产物/状态，包括可信路径、截图/文件/日志/GUI 状态、
被判定不可信或已删除的产物；这些内容会汇聚给后续 executor agent 使用。
必须包含 `给任务管理器的状态更新:` 段落，用自然语言说明本轮新增的可信事实、缺口、
不可信产物和它们对应的任务语境。
"""


# CLI auditors audit command/file/test evidence and may delete confirmed fake
# artifacts through the harness-controlled auditor deletion path. They remain
# read-only; computer access, if available, is observation-only.
LH_HARNESS_CLI_AUDITOR_INSTRUCTIONS = """\
你是 LongHorizon-Harness 的 CLI auditor agent。你只审计刚完成的 CLI/非 GUI 子任务。
你不是 executor agent，不能替它完成任务。

审计边界:
- 只做只读 CLI 审计。不要修改、创建、移动、删除任务文件，除非高置信确认
  某个产物是伪造/作弊/不可信且必须按既有 auditor 删除规则处理。
- 如果有 computer 工具，只能 observe/save_screenshot，不要点击、输入、滚动、拖拽或修改窗口状态。
- 审计重点是命令输出、文件内容、代码修改、测试结果、日志和路径是否满足
  当前 CLI 子任务验收条件。
- 如果当前 CLI 子任务涉及视觉状态，检查 executor agent 是否提供真实 GUI 操作/真实截图证据；
  不能接受只用文件、日志或 headless 输出替代核心视觉状态。
- 不要输出 JSON。

输出紧凑自然语言审计报告。第一行写 `状态: complete`、`状态: incomplete`
或 `状态: blocked`。第二行写 `完整性: clean`、`完整性: suspect` 或
`完整性: violation`。后面写审计事实、证据、缺口和下一步建议。
请明确写出已确认的中间产物/状态，包括可信路径、文件/日志/测试结果/服务状态、
被判定不可信或已删除的产物；这些内容会汇聚给后续 executor agent 使用。
必须包含 `给任务管理器的状态更新:` 段落，用自然语言说明本轮新增的可信事实、缺口、
不可信产物和它们对应的任务语境。
"""


def build_role_manager_prompt(
    *,
    task: str,
    rounds: list[ManagedRound],
    round_index: int,
    task_state: str = "",
    max_history_chars: int = 36_000,
) -> str:
    auditor_reports = format_verified_intermediate_context(rounds, max_chars=max_history_chars)
    harness_feedback = format_harness_feedback_context(rounds)
    return f"""\
{LH_HARNESS_MANAGER_INSTRUCTIONS.strip()}

原始任务:
{task.rstrip()}

上一轮当前任务状态:
{task_state.strip() or "(还没有当前任务状态；请根据原始任务初始化。)"}

历史 auditor 报告原文（按 round 编号；这是可信中间状态的权威来源）:
{auditor_reports or "(还没有 auditor 报告。)"}

harness 任务管理反馈（不是 auditor 审计；只用于修正输出格式或完成请求）:
{harness_feedback or "(没有 harness 任务管理反馈。)"}

现在是任务管理第 {round_index} 轮。请只输出下一步任务管理结果。

要求:
- 先维护并输出 `当前任务状态:`，状态事实必须引用 auditor round；没有 auditor 证据的只能标为待审计。
- 然后输出 `依赖判断:`，必须写明目标状态、状态创建者、已满足前置、未满足前置和本轮路由理由。
- 如果 `未满足前置` 非空，本轮 `任务:` 必须解决其中一个最关键前置，不能直接派最终截图或最终交付任务。
- 不要只按交付物后缀路由；先判断该交付物背后的系统状态由 GUI 创建还是 CLI 创建，以及前置是否已被 auditor 确认。
- 每轮只输出一个给下游 sub agent 的任务合同，具体做法交给 executor agent。
- 子任务必须只有一个主目标形态；辅助工具可以使用，但不能把多个主状态变化混在一起。
- 只写“任务 / 验收标准(可选) / 边界”，不要把多个目标塞进同一轮。
- `相关审计报告:` 必须列出与当前子任务相关的 round id，例如 `round_003`；harness 会按这些 id 把 auditor 原文传给 executor/auditor。
- 如果 auditor 报告指出上一个子任务不完整、拆解混杂或完整性 suspect/violation，优先生成一个更小的修复子任务。
- 如果原始任务包含“询问我 / 让我决定 / 由用户选择”等需要真人拍板的环节，输出 `下一步: 请示用户` 并写 `问题:`，不要拆成子任务去问用户。
- 如果所有原始任务要求已经被 auditor 自然语言报告确认完成且完整性 clean，输出 `下一步: 完成`。
- 不要输出 JSON。
"""


def build_role_executor_prompt(
    *,
    task: str,
    plan_text: str,
    next_step: RoleNextStep,
    task_state: str = "",
    related_auditor_reports: str = "",
) -> str:
    role_name = "GUI/视觉" if next_step == MANAGER_NEXT_GUI else "CLI/非 GUI"
    role_instructions = (
        LH_HARNESS_GUI_EXECUTOR_INSTRUCTIONS
        if next_step == MANAGER_NEXT_GUI
        else LH_HARNESS_CLI_EXECUTOR_INSTRUCTIONS
    )
    return f"""\
{role_instructions.strip()}

原始任务:
{task.rstrip()}

当前任务状态（由任务管理器维护，事实应来自 auditor 报告）:
{task_state.strip() or "(当前没有已维护任务状态。)"}

当前任务管理器分配的 {role_name} 主目标子任务合同:
{plan_text.rstrip()}

相关 auditor 报告原文（由任务管理器在 `相关审计报告:` 中引用，harness 按 round id 加载）:
{related_auditor_reports.strip() or "(任务管理器没有引用相关 auditor 报告。)"}

执行要求:
- 只完成当前子任务。
- 当前任务状态和相关 auditor 报告原文是当前最可信的中间上下文；优先复用其中已确认的可信中间产物和状态。
- 不要重复生成 auditor 已确认完成的产物。
- 不要依赖 auditor 标记为 suspect/violation、伪造、不可信或已删除的产物。
- 如果当前子任务缺少必要上下文，停止并说明需要任务管理器补充 `相关审计报告`，不要猜。
- 不要做另一类任务；如果需要另一类能力，停止并用自然语言说明需要重新拆解。
- 结束时用自然语言报告真实执行过程、当前状态、产物路径和剩余问题。
- 不要输出 JSON。
"""


def build_role_auditor_prompt(
    *,
    task: str,
    plan_text: str,
    executor_output: str,
    next_step: RoleNextStep,
    task_state: str = "",
    related_auditor_reports: str = "",
    max_executor_output_chars: int = 24_000,
) -> str:
    role_name = "GUI/视觉" if next_step == MANAGER_NEXT_GUI else "CLI/非 GUI"
    role_instructions = (
        LH_HARNESS_GUI_AUDITOR_INSTRUCTIONS
        if next_step == MANAGER_NEXT_GUI
        else LH_HARNESS_CLI_AUDITOR_INSTRUCTIONS
    )
    return f"""\
{role_instructions.strip()}

原始任务:
{task.rstrip()}

当前任务状态（由任务管理器维护，作为审计背景；你只审当前子任务）:
{task_state.strip() or "(当前没有已维护任务状态。)"}

刚刚审计的 {role_name} 主目标子任务:
{plan_text.rstrip()}

executor agent 的自然语言输出:
{_clip_preserve(executor_output.rstrip(), max_executor_output_chars)}

相关 auditor 报告原文（作为当前审计背景，不替代你对真实状态的审计）:
{related_auditor_reports.strip() or "(任务管理器没有引用相关 auditor 报告。)"}

请只审计当前子任务是否真实完成，是否保持本轮 {role_name} 主目标边界，是否存在完整性问题。
请在报告中继续明确写出当前仍可信的中间产物/状态、当前新增可信产物、以及不可信或已删除产物。
必须包含 `给任务管理器的状态更新:` 段落。
输出自然语言审计报告，不要输出 JSON。
"""


def build_role_auditor_format_repair_prompt(*, report_text: str) -> str:
    return f"""\
你的上一份 auditor 报告缺少有效的前两行控制头。

这不是重新审计任务。不要使用工具，不要观察屏幕，不要运行命令，不要修改、创建、移动或删除任何文件。
只根据你上一份报告已经写出的审计内容，重新输出同一份报告。

第一行必须严格是以下三种之一:
状态: complete
状态: incomplete
状态: blocked

第二行必须严格是以下三种之一:
完整性: clean
完整性: suspect
完整性: violation

如果你无法仅根据上一份报告确定状态或完整性，请保守输出:
状态: blocked
完整性: suspect

上一份 auditor 报告:
{report_text.strip() or "(空报告)"}

现在只输出修正格式后的 auditor 报告，不要输出 JSON，不要解释这次格式修正过程。
"""


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
            if head.startswith("问题:") or head.startswith("问题："):
                collecting = True
                rest = line.split("：", 1)[-1] if "：" in line else line.split(":", 1)[-1]
                if rest.strip():
                    collected.append(rest.strip())
            continue
        # stop at the next known section header
        if re.match(r"^\s*(?:\*\*)?\s*(?:选项|当前任务状态|依赖判断|下一步|任务|验收标准|相关审计报告|相关已审计状态|边界)\s*[:：]", line):
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
        m = re.match(r"^\s*选项\s*[:：]\s*(.+)$", stripped)
        if m:
            raw = m.group(1)
            parts = re.split(r"\s*[|、,，/]\s*", raw)
            choices = [p.strip() for p in parts if p.strip()]
            if choices:
                return choices[:6]
    question = extract_role_manager_question(plan_text)
    if "是否" in question or ("是" in question and "否" in question):
        return ["是", "否"]
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
    state_start = raw.rfind("当前任务状态", 0, route_start)
    if state_start >= 0:
        return raw[state_start:].strip()
    return raw[route_start:].strip()


_TASK_STATE_HEADER_RE = re.compile(r"(?im)^\s*(?:\*\*)?\s*当前任务状态\s*[:：]")
_TASK_STATE_BOUNDARY_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*(?:依赖判断"
    r"|下一步\s*[:：]\s*(?:GUI任务|CLI任务|请示用户|请示|询问用户|完成|阻塞)"
    r"|next\s*:\s*(?:gui|cli|ask|done|complete|blocked))"
)
_RELATED_REPORT_SECTION_RE = re.compile(
    r"(?ims)^\s*(?:\*\*)?\s*相关(?:审计报告|已审计状态)\s*[:：]\s*(.*?)(?=^\s*(?:\*\*)?\s*(?:边界|任务|验收标准|下一步)\s*[:：]|\Z)"
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
) -> str:
    selected: list[ManagedRound] = []
    ref_numbers = {_round_ref_to_index(item) for item in refs}
    ref_numbers.discard(None)
    for item in rounds:
        if item.round_index in ref_numbers and item.auditor_report.strip():
            selected.append(item)
    return format_verified_intermediate_context(selected, max_chars=max_chars)


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


def format_verified_intermediate_context(rounds: list[ManagedRound], *, max_chars: int = 30_000) -> str:
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
                    "对应子任务:",
                    _clip_preserve(subtask, 1600) if subtask else "(无)",
                    "auditor 报告原文:",
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
