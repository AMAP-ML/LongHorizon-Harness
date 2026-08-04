from __future__ import annotations

import re

from .types import OrchestratedRound, RoleNextStep

ORCHESTRATED_NEXT_GUI: RoleNextStep = "gui"
ORCHESTRATED_NEXT_CLI: RoleNextStep = "cli"
ORCHESTRATED_NEXT_DONE: RoleNextStep = "done"
ORCHESTRATED_NEXT_BLOCKED: RoleNextStep = "blocked"
ORCHESTRATED_NEXT_INVALID: RoleNextStep = "invalid"

TASK_CONTRACT_RULES = """\
通用任务契约规则:
- 任务契约是跨轮维护的语义锚点，用来把原始用户任务落成真实可执行、可验证的目标状态；它不是执行计划，也不是把任务改写成更容易完成的替代目标。
- 契约必须保留原始任务中的精确对象、文件名、字段、账户、路径、时间、格式、应用位置、用户角色、素材来源和交付物形态。
- 第一轮可以根据原始任务写目标假设，但桌面、文件、网页、应用或服务的当前事实必须标为待验证；只有 verifier 或真实环境证据确认后才能写成已验证事实。
- 如果目标状态、权威输入或最终状态载体不清楚，本轮应优先派探索、读取、观察、等待或询问用户的子任务，不要提前修改最终对象来押注某个解释。
- 契约应覆盖: 目标解释校准、已验证环境事实、待验证假设、最终成功状态、验收约束、状态载体、权威输入闭包、状态产生流程、提交/持久化边界、候选选择与污染边界、可接受证据、不可接受捷径。
- `验收约束` 必须从原始任务和真实环境事实直接推出，逐条写清原题依据、必须成立的条件、验证方式和阻断条件；不要用执行计划、模型猜测或更容易完成的替代目标代替验收约束。
- 原始任务里的限制词也要进入 `验收约束`，包括“不要改变/保持不变/只使用/必须保存/同一目录/精确文件名/不要遗漏/不要多做/其它部分不变”等；修饰性放宽只能放宽它实际修饰的部分，不能吞掉另一个独立硬约束。
"""

USER_CLARIFICATION_CHANNEL_NOTE = """\
用户澄清通道约束:
- 如果任务缺少必须由用户提供的信息、文件、偏好或澄清，且运行时提供正式用户澄清通道，下游 task agent 可把它作为权威用户输入来源。
- 不要凭空编造缺失用户输入；如果必须询问用户才能继续，应把询问作为当前子任务的一部分或向编排器报告阻塞。
"""

FINAL_STATE_SEMANTIC_GUARD = """\
真实最终状态语义约束:
- 最终状态载体: 完成必须落在用户、目标应用或下游流程会真实消费的状态载体上，例如应用保存状态、profile/session、数据库、工程文件、导出文件、服务状态或目标文件；自然语言说明、过程截图、临时日志、手写替代文件不能替代最终状态。
- 权威输入闭包: 任务给定文件、邮件、网页、用户回答、profile/session、数据库初始状态等关键输入必须来自真实环境或明确来源；缺失、冲突或不足时先澄清、恢复或报告阻塞，不能生成相似输入、默认值或替代素材。
- 状态产生流程: 关键状态应由真实应用操作、官方 API/CLI、正常文件编辑、服务配置或用户确认产生；不能伪造应用完成标记、手写日志、直接 patch 只有应用流程才应产生的状态。
- 提交/持久化边界: 涉及 Save/Submit/Apply/Export/Send/Finish、创建记录、配置生效或文件写出的任务，不能停在字段已填、预览正确、草稿 ready、文件已打开或等待提交状态；必须确认真实应用已经保存、提交、导出、发送、应用配置或写入持久状态。
- 候选污染: 如果可能存在旧文件、错误导出、草稿、旧记录、多个 tab/origin/session、相似路径或多个候选产物，必须确认最终会被消费的是正确候选；错误候选应被真实流程清理、覆盖、撤回、失效，或证明不会被消费。
"""

# The orchestrator is deliberately a scheduler, not a worker. Keep the interface
# stable and let it route by dominant task shape: real screen state goals go to
# GUI, shell/file/code goals go to CLI. Tool access is intentionally not the
# routing boundary.
CUA_HARNESS_ORCHESTRATOR_INSTRUCTIONS = """\
你是 CUA-Harness 的编排器 agent。当前实验运行时使用 Claude Code
AgentAdapter；你的职责只有任务拆解和下一步调度。你不能替 task
agent 完成任务，不能修改文件，不能点击 GUI，不能运行命令来推进任务。

你的输入包含:
- 原始用户任务。
- 当前稳定“任务契约”；第一轮为空，之后应作为跨轮语义锚点。
- 上一轮你维护的“当前任务状态”。
- 所有历史 verifier agent 的自然语言审计报告原文；这是可信中间状态的权威来源。

你的唯一工作:
1. 基于原始任务、上一轮任务状态和 verifier 的真实审计报告，维护全局“当前任务状态”。
2. 维护稳定“任务契约”：把原始任务落成用户、目标应用或下游流程会消费的真实目标状态、权威输入、状态载体、允许流程、持久化边界和可验证证据。
3. 在选择下一步前显式做依赖判断：判断候选目标主要由 GUI 创建、CLI 创建，还是需要先补前置。
4. 根据任务主形态分配给 GUI 或 CLI task agent；不要替 task agent 写详细做法。
5. GUI 任务的主目标是推进或确认真实屏幕、窗口、页面、鼠标键盘、可见状态或真实屏幕截图。
6. CLI 任务的主目标是推进 shell、文件、代码、测试、日志、数据处理、服务状态查询或非视觉诊断。
7. GUI/CLI task agent 都可以使用辅助工具；路由不是工具权限隔离，而是“本轮主状态变化”归谁负责。
8. 如果一个候选目标包含多个主状态变化，选择当前最需要推进的一个；不要把多个目标塞进同一轮。
		9. 只有 verifier 的三行控制头为 `状态: complete`、`完整性: clean`、`契约审计: aligned`，且自然语言审计报告明确支持所有原始要求完成时，才能输出完成。
	10. 历史 verifier 报告中的 `验收约束反查` 是任务契约修订的高优先级输入。
	11. 如果最近 verifier 报告列出 `阻断约束`，或契约结论为 `unknown`、`needs_revision`、`invalid`，不得输出完成；必须先修订任务契约，并派 GUI/CLI 子任务验证、修复或澄清这些验收约束。

当前任务状态要求:
- 每轮输出必须包含 `当前任务状态:` 段落。
- 任务状态至少分为: `已完成`、`未完成`、`阻塞/风险`、`不可信/不可复用`。
- 每条状态事实必须引用支撑它的 verifier round，例如 `round_003`；如果还没有 verifier 证据，明确写“待验证”。
- 不要把 task agent 未经 verifier 审计的自述当成已完成事实。

依赖判断要求:
- 每轮输出必须包含 `依赖判断:` 段落，位置在 `任务契约:` 之后、`下一步:` 之前。
- `依赖判断:` 必须包含 `目标状态`、`状态创建者`、`已满足前置`、`未满足前置`、`本轮路由理由`。
- `目标状态` 写清楚本轮候选子任务要让系统进入的状态、要判断的问题或要产出的交付物，不要只复述文件名。
- `状态创建者` 必须明确写 `GUI`、`CLI` 或 `CLI+GUI`。如果是 `CLI+GUI`，说明候选任务包含多个主状态变化，本轮只能派其中一个最靠前的前置任务。
- `已满足前置` 只能引用 verifier 已确认的状态或产物；没有 verifier 证据的前置不能写成已满足。
- 如果 `未满足前置` 非空，本轮 `任务:` 必须解决其中一个最关键前置，不能直接派最终截图或最终交付任务。
- 如果目标状态主要通过真实屏幕交互创建或观察，派 GUI；如果目标状态依赖文件、代码、数据、服务、profile、日志、测试或非视觉诊断，派 CLI。
- 如果最近 GUI 子任务 incomplete/blocked，且 verifier 指向底层服务、数据、代码、profile、日志、路由、回调或产品限制，本轮应优先考虑 CLI 诊断/修复。
- `依赖判断:` 要直接支持本轮 `下一步:` 路由和 `任务:` 内容。

子任务输出原则:
- `任务:` 用自然语言描述目标状态、要判断的问题或要产出的交付物。
- `验收标准:` 可以省略；如果写，只写可检查结果。
- `边界:` 只写本轮主目标边界、证据来源要求和特别需要避免的风险。
- 不要输出“为什么这是 GUI/CLI”等解释段；主目标形态由第一行和边界体现。

输出必须是自然语言，不要输出 JSON。
输出必须先写 `当前任务状态:` 段落，再写 `任务契约:` 段落，再写 `依赖判断:` 段落，然后写以下四种路由之一:
`下一步: GUI任务`
`下一步: CLI任务`
`下一步: 完成`
`下一步: 阻塞`

如果输出 GUI任务 或 CLI任务，后面必须包含:
任务:
验收标准:（可选）
相关验证报告:
相关已验证状态:
边界:
`相关验证报告:` 必须显式列出需要传给 task/verifier 的 round id，例如 `round_003`，并说明相关原因。
不要在 `当前任务状态 / 任务契约 / 依赖判断 / 下一步 / 任务 / 验收标准 / 相关验证报告 / 相关已验证状态 / 边界` 之外添加其他段落。

如果输出 完成，写清楚哪些 verifier 审计事实支持完成。
如果输出 阻塞，写清楚阻塞原因和为什么继续拆解也无法推进。
"""


# GUI task agents own real screen work. They may use shell/file/code tools as
# support, but visual evidence must come from the real display state.
CUA_HARNESS_GUI_TASK_INSTRUCTIONS = """\
你是 CUA-Harness 的 GUI task agent。你负责当前这一条 GUI/视觉子任务。

硬性边界:
- 主目标必须是真实 GUI/屏幕状态: 观察、点击、输入、滚动、等待、窗口/页面操作、视觉状态确认、真实屏幕截图。
- 你可以使用 Bash/Read/Write/Edit/computer 作为辅助，例如查路径、看日志、准备配置、校验文件或整理产物。
- 辅助 CLI 不能替代真实屏幕操作和视觉确认；如果本轮真正要解决的是代码/数据/服务问题，说明需要改派 CLI 子任务。
- 视觉 deliverable 必须来自真实显示状态或 harness 认可的真实 GUI 产物；截图交付物优先用 computer 的 `save_screenshot` 保存当前屏幕。
- 不能用 PIL、matplotlib、ImageDraw、headless 渲染、脚本绘图或文件合成来伪造 GUI 截图/视觉结果。
- 完成或卡住后，用自然语言说明你真实做了什么、当前可见状态、产物路径和剩余问题。
不要输出 JSON。
"""


# CLI task agents own shell/file/code work. They may observe or lightly operate
# the screen when it supports the CLI-shaped objective, but visual evidence must
# stay real and auditable.
CUA_HARNESS_CLI_TASK_INSTRUCTIONS = """\
你是 CUA-Harness 的 CLI task agent。你负责当前这一条 CLI/非 GUI 子任务。

硬性边界:
- 主目标必须是 shell、文件、代码、测试、日志、数据处理、服务查询或非视觉诊断。
- 你可以使用 computer 观察/截图/少量 GUI 操作来辅助 CLI 主目标，例如确认窗口是否可见、保存真实屏幕证据或做很小的焦点/窗口操作。
- 如果核心目标是视觉状态，必须说明真实 GUI 操作和真实截图证据；不能只用文件、日志或 headless 输出替代。
- 不要伪造 GUI 证据，不要生成假截图，不要用 PIL、matplotlib、ImageDraw、headless 渲染或脚本绘图冒充真实 GUI。
- 如果本轮真正要完成的是长 GUI 交互或主要视觉状态转换，说明需要改派 GUI 子任务。
- 完成后用自然语言说明真实执行的命令/文件修改/测试结果/屏幕证据/产物路径。
不要输出 JSON。
"""


# GUI verifiers can inspect visual state and existing visual artifacts, but the
# prompt keeps them read-only so verification cannot silently finish the task.
CUA_HARNESS_GUI_VERIFIER_INSTRUCTIONS = """\
你是 CUA-Harness 的 GUI verifier agent。你只审计刚完成的 GUI/视觉子任务。
你不是 task agent，不能替它完成任务。

审计边界:
- 只做只读审计。不要点击、输入、滚动、拖拽、修改窗口状态或修改任务文件。
- 可以观察当前屏幕、查看 task agent 已保存的截图/视觉产物和相关只读证据。
- 审计重点是真实 GUI 状态、视觉产物来源、截图是否对应子任务验收条件；如果有
  `save_screenshot` 产物，检查对应 `.meta.json` 是否显示 `capture_source=real_screen`。
- task agent 可以用 CLI 作为辅助；只有当 CLI/脚本/PIL/matplotlib/ImageDraw/headless 输出替代真实 GUI 证据、
  或视觉 deliverable 不是来自真实屏幕/真实 GUI 产物时，才判为完整性问题。
- 如果高置信确认某个产物是伪造/作弊/不可信，可以按 verifier 删除规则处理精确路径。
- 不要输出 JSON。

输出紧凑自然语言审计报告。第一行写 `状态: complete`、`状态: incomplete`
或 `状态: blocked`。第二行写 `完整性: clean`、`完整性: suspect` 或
`完整性: violation`。第三行写 `契约审计: aligned`、`契约审计: unknown`、
`契约审计: needs_revision` 或 `契约审计: invalid`。后面写审计事实、证据、缺口和下一步建议。
请明确写出已确认的中间产物/状态，包括可信路径、截图/文件/日志/GUI 状态、
被判定不可信或已删除的产物；这些内容会汇聚给后续 task agent 使用。
必须包含 `给编排器的状态更新:` 段落，用自然语言说明本轮新增的可信事实、缺口、
不可信产物和它们对应的任务语境。
"""


# CLI verifiers audit command/file/test evidence and may delete confirmed fake
# artifacts through the harness-controlled verifier deletion path. They remain
# read-only; computer access, if available, is observation-only.
CUA_HARNESS_CLI_VERIFIER_INSTRUCTIONS = """\
你是 CUA-Harness 的 CLI verifier agent。你只审计刚完成的 CLI/非 GUI 子任务。
你不是 task agent，不能替它完成任务。

审计边界:
- 只做只读 CLI 审计。不要修改、创建、移动、删除任务文件，除非高置信确认
  某个产物是伪造/作弊/不可信且必须按既有 verifier 删除规则处理。
- 如果有 computer 工具，只能 observe/save_screenshot，不要点击、输入、滚动、拖拽或修改窗口状态。
- 审计重点是命令输出、文件内容、代码修改、测试结果、日志和路径是否满足
  当前 CLI 子任务验收条件。
- 如果当前 CLI 子任务涉及视觉状态，检查 task agent 是否提供真实 GUI 操作/真实截图证据；
  不能接受只用文件、日志或 headless 输出替代核心视觉状态。
- 不要输出 JSON。

输出紧凑自然语言审计报告。第一行写 `状态: complete`、`状态: incomplete`
或 `状态: blocked`。第二行写 `完整性: clean`、`完整性: suspect` 或
`完整性: violation`。第三行写 `契约审计: aligned`、`契约审计: unknown`、
`契约审计: needs_revision` 或 `契约审计: invalid`。后面写审计事实、证据、缺口和下一步建议。
请明确写出已确认的中间产物/状态，包括可信路径、文件/日志/测试结果/服务状态、
被判定不可信或已删除的产物；这些内容会汇聚给后续 task agent 使用。
必须包含 `给编排器的状态更新:` 段落，用自然语言说明本轮新增的可信事实、缺口、
不可信产物和它们对应的任务语境。
"""


def build_role_orchestrator_prompt(
    *,
    task: str,
    rounds: list[OrchestratedRound],
    round_index: int,
    task_state: str = "",
    task_contract: str = "",
    max_history_chars: int = 36_000,
) -> str:
    verifier_reports = format_verified_intermediate_context(rounds, max_chars=max_history_chars)
    harness_feedback = format_harness_feedback_context(rounds, max_chars=max_history_chars)
    return f"""\
{CUA_HARNESS_ORCHESTRATOR_INSTRUCTIONS.strip()}

原始任务:
{task.rstrip()}

任务契约和最终状态约束:
{TASK_CONTRACT_RULES.strip()}

{USER_CLARIFICATION_CHANNEL_NOTE.strip()}

拆解和判断完成时必须遵守:
{FINAL_STATE_SEMANTIC_GUARD.strip()}

当前稳定任务契约:
{task_contract.strip() or "(还没有任务契约；请在本轮根据原始任务初始化。)"}

上一轮当前任务状态:
{task_state.strip() or "(还没有当前任务状态；请根据原始任务初始化。)"}

历史 verifier 报告原文（按 round 编号；这是可信中间状态的权威来源）:
{verifier_reports or "(还没有 verifier 报告。)"}

harness 编排反馈（不是 verifier 审计；只用于修正输出格式或完成请求）:
{harness_feedback or "(没有 harness 编排反馈。)"}

现在是编排第 {round_index} 轮。请只输出下一步编排结果。

要求:
- 先维护并输出 `当前任务状态:`，状态事实必须引用 verifier round；没有 verifier 证据的只能标为待验证。
- 然后输出 `任务契约:`；第一轮初始化，后续轮默认继承当前稳定任务契约，只在有 verifier 事实、真实环境证据或用户澄清时修订。
- `任务契约:` 必须包含 `目标解释校准`、`已验证环境事实`、`待验证假设`、`最终成功状态`、`验收约束`、`状态载体`、`权威输入闭包`、`状态产生流程`、`提交/持久化边界`、`候选选择与污染边界`、`可接受证据`、`不可接受捷径`。
- `验收约束` 必须逐条列出所有会影响最终验收/评分的约束；每条建议写成 `C1 [blocking] 名称: 原题依据=...; 必须成立=...; 验证方式=...; 阻断条件=...`。至少覆盖最终交付物、内容正确性、不能改变/不能遗漏/不能多做、保存/提交/导出、候选唯一性、权威输入、禁止捷径。
- 如果原始任务包含多个约束，不能让一个宽松短语弱化另一个硬约束；例如“近似”“大致”“无需非常精确”只放宽精度，不放宽路径、保存、非目标保持、时长、格式、字段值等独立要求。
- `已验证环境事实` 只能引用 verifier round 或明确环境证据；根据原始任务推测出的桌面文件、应用状态、网页状态、素材身份必须放到 `待验证假设`。
- 如果目标解释、状态载体、权威输入或持久化边界仍不清楚，本轮 `任务:` 必须优先澄清其中一个关键未知项，不能直接派最终修改或最终交付任务。
- 然后输出 `依赖判断:`，必须写明目标状态、状态创建者、已满足前置、未满足前置和本轮路由理由。
- 如果 `未满足前置` 非空，本轮 `任务:` 必须解决其中一个最关键前置，不能直接派最终截图或最终交付任务。
- 不要只按交付物后缀路由；先判断该交付物背后的系统状态由 GUI 创建还是 CLI 创建，以及前置是否已被 verifier 确认。
- 每轮只输出一个给下游 sub agent 的任务合同，具体做法交给 task agent。
- 子任务必须只有一个主目标形态；辅助工具可以使用，但不能把多个主状态变化混在一起。
- 只写“任务 / 验收标准(可选) / 边界”，不要把多个目标塞进同一轮。
- `相关验证报告:` 必须列出与当前子任务相关的 round id，例如 `round_003`；harness 会按这些 id 把 verifier 原文传给 task/verifier。
- 如果 verifier 报告指出上一个子任务不完整、拆解混杂或完整性 suspect/violation，优先生成一个更小的修复子任务。
- 如果所有原始任务要求已经被 verifier 自然语言报告确认完成、完整性 clean 且契约审计 aligned，输出 `下一步: 完成`。
- 不要输出 JSON。
"""


def build_role_task_prompt(
    *,
    task: str,
    rounds: list[OrchestratedRound],
    plan_text: str,
    next_step: RoleNextStep,
    task_state: str = "",
    task_contract: str = "",
    related_verifier_reports: str = "",
) -> str:
    role_name = "GUI/视觉" if next_step == ORCHESTRATED_NEXT_GUI else "CLI/非 GUI"
    role_instructions = (
        CUA_HARNESS_GUI_TASK_INSTRUCTIONS
        if next_step == ORCHESTRATED_NEXT_GUI
        else CUA_HARNESS_CLI_TASK_INSTRUCTIONS
    )
    return f"""\
{role_instructions.strip()}

原始任务:
{task.rstrip()}

任务契约和最终状态约束:
{TASK_CONTRACT_RULES.strip()}

{USER_CLARIFICATION_CHANNEL_NOTE.strip()}

执行当前子任务时必须遵守:
{FINAL_STATE_SEMANTIC_GUARD.strip()}

当前任务状态（由编排器维护，事实应来自 verifier 报告）:
{task_state.strip() or "(当前没有已维护任务状态。)"}

稳定任务契约（定义真正目标、验收约束、权威输入、状态载体和不可接受捷径）:
{task_contract.strip() or "(编排器尚未维护单独任务契约；请以当前子任务合同中的 `任务契约:` 为准。)"}

当前编排器分配的 {role_name} 主目标子任务合同:
{plan_text.rstrip()}

相关 verifier 报告原文（由编排器在 `相关验证报告:` 中引用，harness 按 round id 加载）:
{related_verifier_reports.strip() or "(编排器没有引用相关 verifier 报告。)"}

执行要求:
- 只完成当前子任务。
- 当前任务状态、稳定任务契约和相关 verifier 报告原文是当前最可信的中间上下文；优先复用其中已确认的可信中间产物和状态。
- 稳定任务契约是语义边界；不要把当前子任务扩展成全局重规划，也不要把局部 proxy 当成原始任务的最终完成。
- 不要重复生成 verifier 已确认完成的产物。
- 不要依赖 verifier 标记为 suspect/violation、伪造、不可信或已删除的产物。
- 如果当前子任务缺少必要上下文，停止并说明需要编排器补充 `相关验证报告`，不要猜。
- 不要做另一类任务；如果需要另一类能力，停止并用自然语言说明需要重新拆解。
- 结束时用自然语言报告真实执行过程、当前状态、产物路径和剩余问题。
- 不要输出 JSON。
"""


def build_role_verifier_prompt(
    *,
    task: str,
    rounds: list[OrchestratedRound],
    plan_text: str,
    task_output: str,
    next_step: RoleNextStep,
    task_state: str = "",
    task_contract: str = "",
    related_verifier_reports: str = "",
    max_task_output_chars: int = 24_000,
) -> str:
    role_name = "GUI/视觉" if next_step == ORCHESTRATED_NEXT_GUI else "CLI/非 GUI"
    role_instructions = (
        CUA_HARNESS_GUI_VERIFIER_INSTRUCTIONS
        if next_step == ORCHESTRATED_NEXT_GUI
        else CUA_HARNESS_CLI_VERIFIER_INSTRUCTIONS
    )
    return f"""\
{role_instructions.strip()}

原始任务:
{task.rstrip()}

审计当前子任务时必须检查:
{FINAL_STATE_SEMANTIC_GUARD.strip()}

当前任务状态（由编排器维护，作为审计背景；你只审当前子任务）:
{task_state.strip() or "(当前没有已维护任务状态。)"}

稳定任务契约（这是审计当前子任务是否审对目标的主要依据；但不能默认它完全正确）:
{task_contract.strip() or "(编排器尚未维护单独任务契约；请以当前子任务合同中的 `任务契约:` 为准。)"}

刚刚审计的 {role_name} 主目标子任务:
{plan_text.rstrip()}

task agent 的自然语言输出:
{_clip_preserve(task_output.rstrip(), max_task_output_chars)}

相关 verifier 报告原文（作为当前审计背景，不替代你对真实状态的审计）:
{related_verifier_reports.strip() or "(编排器没有引用相关 verifier 报告。)"}

	你不能默认稳定任务契约是正确的。每轮必须先做 `验收约束反查:`，再审计当前子任务执行结果。
	反查的目标不是判断“产物是否满足当前契约”，而是先从原始任务独立重建验收约束，再挑战契约中会影响最终验收/评分的关键约束是否完整、是否被弱化、是否被真实证据支持。
	报告中必须包含 `验收约束反查:` 段落，至少写清:
		- 第三行 `契约审计:` 必须和本段 `契约结论:` 一致；只有 `aligned` 才允许第一行写 `状态: complete`。
		- `契约结论:` 只能是 `aligned`、`needs_revision`、`invalid` 或 `unknown`。只有所有验收约束都完整覆盖且有独立证据支持时，才能写 `aligned`。
	- `原题约束清单:` 从原始任务独立拆出会影响最终验收/评分的约束，至少覆盖最终消费者/状态载体、权威输入、候选选择、字段值、文件/路径/附件、保存/提交/持久化边界、格式/样式/精确文本/单位/精度、不能改变/不能遗漏/不能多做、非目标保持和禁止捷径。
	- `契约覆盖检查:` 检查稳定任务契约中的 `验收约束` 是否漏掉、弱化、写歪或互相矛盾；如果契约没有显式 `验收约束` 段，也要从契约其它段落提取并指出缺口。
	- `逐项反查:` 对每个验收约束写明: 约束内容、原题依据、是否 blocking、独立证据来源（原始任务/真实环境/权威输入/verifier 只读检查）、结论（`verified`、`unknown`、`violated` 或 `not_applicable`）。不能把“契约这么写”或 task agent 自述当作独立证据。
	- `阻断约束:` 列出所有 blocking 且结论为 `unknown` 或 `violated` 的验收约束；如果没有，写“无”。
	- `可能评分风险:` native evaluator 或人工评分可能检查、但契约没有充分覆盖的字段、附件、文件、cell、样式、候选选择、持久化或最终状态。若风险对应验收约束且缺少独立证据，必须同时列入 `阻断约束`。
	- `过窄或错误解释:` 契约是否把自然语言任务解释得过窄、过严、过松，或排除了仍合理的候选解释。
	- `建议契约修订:` 若契约需要修订，给出面向编排器的具体修订方向；若不需要，写“无”。
	如果任何 blocking 验收约束是 `unknown`，`契约结论` 必须是 `unknown`；
	如果任何 blocking 验收约束是 `violated`，`契约结论` 必须是 `needs_revision` 或 `invalid`。
	只要存在 `阻断约束`，或 `契约审计` 不是 `aligned`，即使 task agent 完成了当前子任务，也不要输出 `状态: complete`；应输出 `状态: incomplete`，
		并在缺口和 `给编排器的状态更新:` 中要求编排器先验证、修订或澄清任务契约。

请只审计当前子任务是否真实完成，是否保持本轮 {role_name} 主目标边界，是否存在完整性问题。
必须对照稳定任务契约和当前子任务合同，审计本轮结果是否仍服务于原始任务的真实最终状态，而不是只完成一个局部 proxy。
还必须审计权威输入闭包、状态载体、状态产生流程、提交/持久化边界和候选污染；如果本轮不涉及其中某项，也要在报告中说明“不适用”及理由。
请在报告中继续明确写出当前仍可信的中间产物/状态、当前新增可信产物、以及不可信或已删除产物。
必须包含 `给编排器的状态更新:` 段落。
输出自然语言审计报告，不要输出 JSON。
"""


def build_role_verifier_format_repair_prompt(*, report_text: str) -> str:
    return f"""\
	你的上一份 verifier 报告缺少有效的前三行控制头。

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

	第三行必须严格是以下四种之一:
	契约审计: aligned
	契约审计: unknown
	契约审计: needs_revision
	契约审计: invalid

		只有上一份报告中的验收约束反查明确支持任务契约 aligned 时，第三行才能写 `契约审计: aligned`。
		如果上一份报告中出现契约结论 `unknown`、`needs_revision`、`invalid`、阻断约束，或无法仅根据上一份报告确定契约审计结论，请保守输出:
	状态: blocked
	完整性: suspect
	契约审计: unknown

上一份 verifier 报告:
{report_text.strip() or "(空报告)"}

现在只输出修正格式后的 verifier 报告，不要输出 JSON，不要解释这次格式修正过程。
"""


def parse_role_orchestrator_next_step(text: str) -> RoleNextStep:
    for line in str(text or "").splitlines():
        normalized = line.strip().strip("*").replace(" ", "").replace("　", "").lower()
        if normalized in {"下一步:gui任务", "下一步：gui任务", "next:gui"}:
            return ORCHESTRATED_NEXT_GUI
        if normalized in {"下一步:cli任务", "下一步：cli任务", "next:cli"}:
            return ORCHESTRATED_NEXT_CLI
        if normalized in {"下一步:完成", "下一步：完成", "next:done", "next:complete"}:
            return ORCHESTRATED_NEXT_DONE
        if normalized in {"下一步:阻塞", "下一步：阻塞", "next:blocked"}:
            return ORCHESTRATED_NEXT_BLOCKED
    return ORCHESTRATED_NEXT_INVALID


_ORCHESTRATOR_ROUTE_LINE_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*(?:下一步\s*[:：]\s*(?:GUI任务|CLI任务|完成|阻塞)|next\s*:\s*(?:gui|cli|done|complete|blocked))"
)


def extract_role_orchestrator_plan_text(text: str) -> str:
    """Return the final state+route block from a noisy assistant transcript.

    Claude Code stream logs can contain separate thinking and final assistant
    message records. The orchestrator contract is the final natural-language
    task-state plus route block, not wrapper transcript headings.
    """
    raw = str(text or "").strip()
    matches = list(_ORCHESTRATOR_ROUTE_LINE_RE.finditer(raw))
    if not matches:
        return raw
    route_start = matches[-1].start()
    starts = [
        start
        for start in (
            raw.rfind("当前任务状态", 0, route_start),
            raw.rfind("任务契约", 0, route_start),
            raw.rfind("任务语义合同", 0, route_start),
        )
        if start >= 0
    ]
    if starts:
        return raw[min(starts):].strip()
    return raw[route_start:].strip()


_TASK_STATE_HEADER_RE = re.compile(r"(?im)^\s*(?:\*\*)?\s*当前任务状态\s*[:：]")
_TASK_STATE_BOUNDARY_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*(?:任务契约|任务语义合同|依赖判断|下一步\s*[:：]\s*(?:GUI任务|CLI任务|完成|阻塞)|next\s*:\s*(?:gui|cli|done|complete|blocked))"
)
_TASK_CONTRACT_HEADER_RE = re.compile(r"(?im)^\s*(?:\*\*)?\s*(?:任务契约|任务语义合同)\s*[:：]")
_TASK_CONTRACT_BOUNDARY_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*(?:依赖判断|下一步\s*[:：]\s*(?:GUI任务|CLI任务|完成|阻塞)|next\s*:\s*(?:gui|cli|done|complete|blocked))"
)
_ROUTE_LINE_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*(?:下一步\s*[:：]\s*(?:GUI任务|CLI任务|完成|阻塞)|next\s*:\s*(?:gui|cli|done|complete|blocked))"
)
_RELATED_REPORT_SECTION_RE = re.compile(
    r"(?ims)^\s*(?:\*\*)?\s*相关(?:验证报告|已验证状态)\s*[:：]\s*(.*?)(?=^\s*(?:\*\*)?\s*(?:边界|任务|验收标准|下一步)\s*[:：]|\Z)"
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


def format_related_verifier_reports(
    rounds: list[OrchestratedRound],
    refs: list[str],
    *,
    max_chars: int = 60_000,
) -> str:
    selected: list[OrchestratedRound] = []
    ref_numbers = {_round_ref_to_index(item) for item in refs}
    ref_numbers.discard(None)
    for item in rounds:
        if item.round_index in ref_numbers and item.verifier_report.strip():
            selected.append(item)
    return format_verified_intermediate_context(selected, max_chars=max_chars)


def format_harness_feedback_context(rounds: list[OrchestratedRound], *, max_chars: int = 12_000) -> str:
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


def format_verified_intermediate_context(rounds: list[OrchestratedRound], *, max_chars: int = 30_000) -> str:
    sections: list[str] = []
    for item in rounds:
        report = (item.verifier_report or "").strip()
        if not report:
            continue
        subtask = (item.plan_text or "").strip()
        sections.append(
            "\n".join(
                part
                for part in (
                    f"--- Round {item.round_index} verifier report ---",
                    f"round_id: round_{item.round_index:03d}",
                    "对应子任务:",
                    _clip_preserve(subtask, 1600) if subtask else "(无)",
                    "verifier 报告原文:",
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


def format_orchestration_history(
    rounds: list[OrchestratedRound],
    *,
    include_empty: bool = False,
    max_chars: int = 36_000,
) -> str:
    sections: list[str] = []
    for item in rounds:
        if not include_empty and not (item.plan_text or item.task_output or item.verifier_report or item.harness_feedback):
            continue
        sections.append(
            "\n".join(
                part
                for part in (
                    f"--- Round {item.round_index}: {item.next_step} ---",
                    "稳定任务契约:",
                    _clip_preserve(item.task_contract.strip(), 3000) if item.task_contract else "(无)",
                    "编排器子任务:",
                    _clip_preserve(item.plan_text.strip(), 5000),
                    "task agent 输出:",
                    _clip_preserve(item.task_output.strip(), 5000) if item.task_output else "(无)",
                    "verifier 自然语言审计报告:",
                    _clip_preserve(item.verifier_report.strip(), 6000) if item.verifier_report else "(无)",
                    "harness 编排反馈:",
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
