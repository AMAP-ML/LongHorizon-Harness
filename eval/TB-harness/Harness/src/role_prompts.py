from __future__ import annotations

import re

from harness_types import OrchestratedRound, RoleNextStep

ORCHESTRATED_NEXT_CLI: RoleNextStep = "cli"
ORCHESTRATED_NEXT_DONE: RoleNextStep = "done"
ORCHESTRATED_NEXT_BLOCKED: RoleNextStep = "blocked"
ORCHESTRATED_NEXT_INVALID: RoleNextStep = "invalid"

DEFAULT_ORCHESTRATOR_CONTRACT_REQUIREMENTS = """\
- 然后输出 `任务契约:`；第一轮初始化，后续轮默认继承当前稳定任务契约，只在有 verifier 事实、真实环境证据或任务文件证据时修订。
- `任务契约:` 必须包含 `目标解释校准`、`已验证环境事实`、`待验证假设`、`最终成功状态`、`验收约束`、`状态载体`、`权威输入闭包`、`状态产生流程`、`提交/持久化边界`、`候选选择与污染边界`、`可接受证据`、`不可接受捷径`。
- `验收约束` 必须逐条列出所有会影响最终验收/评分的约束；每条建议写成 `C1 [blocking] 名称: 原题依据=...; 必须成立=...; 验证方式=...; 阻断条件=...`。至少覆盖最终交付物、内容正确性、不能改变/不能遗漏/不能多做、保存/提交/导出、候选唯一性、权威输入、禁止捷径。
- 如果原始任务包含多个约束，不能让一个宽松短语弱化另一个硬约束；例如“近似”“大致”“无需非常精确”只放宽精度，不放宽路径、保存、非目标保持、时长、格式、字段值等独立要求。
- `已验证环境事实` 只能引用 verifier round 或明确环境证据；根据原始任务推测出的文件、工程、服务、数据状态必须放到 `待验证假设`。
- 如果目标解释、状态载体、权威输入或持久化边界仍不清楚，本轮 `任务:` 必须优先通过 CLI 读取、检查、测试或等待环境状态来补证其中一个关键未知项；环境无法提供时报告阻塞，不能直接派最终修改或最终交付任务。
"""

TASK_CONTRACT_RULES = """\
通用任务契约规则:
- 任务契约是跨轮维护的语义锚点，用来把原始用户任务落成真实可执行、可验证的目标状态；它不是执行计划，也不是把任务改写成更容易完成的替代目标。
- 契约必须保留原始任务中的精确对象、文件名、字段、账户、路径、时间、格式、素材来源和交付物形态。
- 第一轮可以根据原始任务写目标假设，但文件、工程、数据、服务或环境当前事实必须标为待验证；只有 verifier 或真实环境证据确认后才能写成已验证事实。
- 如果目标状态、权威输入或最终状态载体不清楚，本轮应优先派探索、读取、检查或等待环境状态的 CLI 子任务；如果任务定义和真实环境仍无法提供必要输入，报告阻塞，不要提前修改最终对象来押注某个解释。
- 契约应覆盖: 目标解释校准、已验证环境事实、待验证假设、最终成功状态、验收约束、状态载体、权威输入闭包、状态产生流程、提交/持久化边界、候选选择与污染边界、可接受证据、不可接受捷径。
- `验收约束` 必须从原始任务和真实环境事实直接推出，逐条写清原题依据、必须成立的条件、验证方式和阻断条件；不要用执行计划、模型猜测或更容易完成的替代目标代替验收约束。
- 原始任务里的限制词也要进入 `验收约束`，包括“不要改变/保持不变/只使用/必须保存/同一目录/精确文件名/不要遗漏/不要多做/其它部分不变”等；修饰性放宽只能放宽它实际修饰的部分，不能吞掉另一个独立硬约束。
"""

# The orchestrator is deliberately a scheduler, not a worker. For Terminal-Bench
# style tasks the harness is CLI-only: every executable subtask is shell, file,
# code, data, test, or log work.
CUA_HARNESS_ORCHESTRATOR_INSTRUCTIONS = """\
你是 CUA-Harness 的编排器 agent。当前实验运行时通过 AgentAdapter
调用当前后端；你的职责只有任务拆解和下一步调度。你不能替 task
agent 完成任务，不能修改文件，不能运行命令来推进任务。

你的输入包含:
- 原始用户任务。
- 当前稳定“任务契约”；第一轮为空，之后应作为跨轮语义锚点。
- 上一轮你维护的“当前任务状态”。
- 所有历史 verifier agent 的自然语言审计报告原文；这是可信中间状态的权威来源。

你的唯一工作:
1. 基于原始任务、上一轮任务状态和 verifier 的真实审计报告，维护全局“当前任务状态”。
2. 维护稳定“任务契约”：把原始任务落成用户、目标程序或下游流程会消费的真实目标状态、权威输入、状态载体、允许流程、持久化边界和可验证证据。
3. 在选择下一步前显式做依赖判断：判断候选目标的前置是否已被 verifier 确认，还是需要先补前置。
4. 每轮只能分配一个 CLI task agent 子任务；不要替 task agent 写详细做法。
5. CLI 任务的主目标是推进 shell、文件、代码、测试、日志、数据处理、服务状态查询或非交互式诊断。
6. 如果一个候选目标包含多个主状态变化，选择当前最需要推进的一个；不要把多个目标塞进同一轮。
7. 只有 verifier 的三行控制头为 `状态: complete`、`完整性: clean`、`契约审计: aligned`，且自然语言审计报告明确支持所有原始要求完成时，才能输出完成。
8. 历史 verifier 报告中的 `验收约束反查` 是任务契约修订的高优先级输入。
9. 如果最近 verifier 报告列出 `阻断约束`，或契约结论为 `unknown`、`needs_revision`、`invalid`，不得输出完成；必须先修订任务契约，并派 CLI 子任务验证、修复或补充这些验收约束的环境证据；必要输入缺失时报告阻塞。

当前任务状态要求:
- 每轮输出必须包含 `当前任务状态:` 段落。
- 任务状态至少分为: `已完成`、`未完成`、`阻塞/风险`、`不可信/不可复用`。
- 每条状态事实必须引用支撑它的 verifier round，例如 `round_003`；如果还没有 verifier 证据，明确写“待验证”。
- 不要把 task agent 未经 verifier 审计的自述当成已完成事实。

依赖判断要求:
- 每轮输出必须包含 `依赖判断:` 段落，位置在 `任务契约:` 之后、`下一步:` 之前。
- `依赖判断:` 必须包含 `目标状态`、`状态创建者`、`已满足前置`、`未满足前置`、`本轮路由理由`。
- `目标状态` 写清楚本轮候选子任务要让系统进入的状态、要判断的问题或要产出的交付物，不要只复述文件名。
- `状态创建者` 必须明确写 `CLI`。如果候选目标不是 CLI 可验证目标，改写为可由 CLI 检查或产出的前置状态。
- `已满足前置` 只能引用 verifier 已确认的状态或产物；没有 verifier 证据的前置不能写成已满足。
- 如果 `未满足前置` 非空，本轮 `任务:` 必须解决其中一个最关键前置，不能直接派最终交付任务。
- 目标状态必须依赖文件、代码、数据、服务、profile、日志、测试或非交互式诊断。
- `依赖判断:` 要直接支持本轮 `下一步:` 路由和 `任务:` 内容。

子任务输出原则:
- `任务:` 用自然语言描述目标状态、要判断的问题或要产出的交付物。
- `验收标准:` 可以省略；如果写，只写可检查结果。
- `边界:` 只写本轮主目标边界、证据来源要求和特别需要避免的风险。
- 不要输出额外解释段；主目标形态由第一行和边界体现。

输出必须是自然语言，不要输出 JSON。
输出必须先写 `当前任务状态:` 段落，再写 `任务契约:` 段落，再写 `依赖判断:` 段落，然后写以下三种路由之一:
`下一步: CLI任务`
`下一步: 完成`
`下一步: 阻塞`

如果输出 CLI任务，后面必须包含:
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


# CLI task agents own shell/file/code work.
CUA_HARNESS_CLI_TASK_INSTRUCTIONS = """\
你是 CUA-Harness 的 CLI task agent。你负责当前这一条 CLI 子任务。

硬性边界:
- 你只接收本轮最小子任务包；看不到原始任务全文和完整稳定任务契约是刻意设计。
- 本轮最小子任务包是唯一执行范围；不要从目录名、文件名、历史报告或环境线索主动反推并完成更大的全局任务。
- 主目标必须是 shell、文件、代码、测试、日志、数据处理、服务查询或非交互式诊断。
- 只能通过 CLI 可检查的命令、文件、日志、测试或数据证据完成子任务。
- 不要伪造证据，不要生成与任务无关的替代产物。
- 如果当前子任务缺少必要输入、权威事实或边界，停止并说明缺口；不要猜测全局目标。
- 完成后用自然语言说明真实执行的命令、文件修改、测试结果、日志证据和产物路径。
不要输出 JSON。
"""


# CLI verifiers audit command/file/test evidence and may delete confirmed fake
# artifacts through the harness-controlled verifier deletion path.
CUA_HARNESS_CLI_VERIFIER_BASE_INSTRUCTIONS = """\
你是 CUA-Harness 的 CLI verifier agent。你只审计刚完成的 CLI 子任务。
你不是 task agent，不能替它完成任务。

审计边界:
- 只做只读 CLI 审计。不要修改、创建、移动、删除任务文件，除非高置信确认
  某个产物是伪造/作弊/不可信且必须按既有 verifier 删除规则处理。
- 审计重点是命令输出、文件内容、代码修改、测试结果、日志和路径是否满足
  当前 CLI 子任务验收条件。
- 只能根据 CLI 可检查的命令、文件、日志、测试或数据证据审计。
- 不要输出 JSON。
"""

CUA_HARNESS_CLI_VERIFIER_OUTPUT_INSTRUCTIONS = """\
输出紧凑自然语言审计报告。第一行写 `状态: complete`、`状态: incomplete`
或 `状态: blocked`。第二行写 `完整性: clean`、`完整性: suspect` 或
`完整性: violation`。第三行写 `契约审计: aligned`、`契约审计: unknown`、
`契约审计: needs_revision` 或 `契约审计: invalid`。后面写审计事实、证据、缺口和下一步建议。
请明确写出已确认的中间产物/状态，包括可信路径、文件、日志、测试结果、服务状态、
被判定不可信或已删除的产物；这些内容会汇聚给后续 task agent 使用。
必须包含 `给编排器的状态更新:` 段落，用自然语言说明本轮新增的可信事实、缺口、
不可信产物和它们对应的任务语境。
"""

CUA_HARNESS_CLI_VERIFIER_INSTRUCTIONS = (
    f"{CUA_HARNESS_CLI_VERIFIER_BASE_INSTRUCTIONS.strip()}\n\n"
    f"{CUA_HARNESS_CLI_VERIFIER_OUTPUT_INSTRUCTIONS.strip()}"
)

VERIFIER_BLOCKER_AND_RISK_GUIDANCE = """\
- `阻断约束:` 列出所有 blocking 且结论为 `unknown` 或 `violated` 的验收约束；如果没有，写“无”。
- `可能评分风险:` native evaluator 或人工评分可能检查、但契约没有充分覆盖的字段、附件、文件、cell、样式、候选选择、持久化或最终状态。若风险对应验收约束且缺少独立证据，必须同时列入 `阻断约束`。
"""

DEFAULT_VERIFIER_CONTRACT_COMPLETION_GUIDANCE = """\
如果任何 blocking 验收约束是 `unknown`，`契约结论` 必须是 `unknown`；
如果任何 blocking 验收约束是 `violated`，`契约结论` 必须是 `needs_revision` 或 `invalid`。
只要存在 `阻断约束`，或 `契约审计` 不是 `aligned`，即使 task agent 完成了当前子任务，也不要输出 `状态: complete`；应输出 `状态: incomplete`，
并在缺口和 `给编排器的状态更新:` 中要求编排器先通过 CLI 证据验证、修订任务契约或报告阻塞。
"""


def build_role_orchestrator_prompt(
    *,
    task: str,
    rounds: list[OrchestratedRound],
    round_index: int,
    task_state: str = "",
    task_contract: str = "",
    max_history_chars: int = 36_000,
    prompt_variant: str = "claude-code",
) -> str:
    verifier_reports = format_verified_intermediate_context(rounds, max_chars=max_history_chars)
    harness_feedback = format_harness_feedback_context(rounds, max_chars=max_history_chars)
    del prompt_variant
    orchestrator_instructions = CUA_HARNESS_ORCHESTRATOR_INSTRUCTIONS
    contract_rules = TASK_CONTRACT_RULES
    contract_requirements = DEFAULT_ORCHESTRATOR_CONTRACT_REQUIREMENTS
    return f"""\
{orchestrator_instructions.strip()}

原始任务:
{task.rstrip()}

任务契约和最终状态约束:
{contract_rules.strip()}

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
{contract_requirements.strip()}
- 然后输出 `依赖判断:`，必须写明目标状态、状态创建者、已满足前置、未满足前置和本轮路由理由。
- 如果 `未满足前置` 非空，本轮 `任务:` 必须解决其中一个最关键前置，不能直接派最终交付任务。
- 不要只按交付物后缀拆解；先判断该交付物背后的 CLI 可验证状态，以及前置是否已被 verifier 确认。
- 每轮只输出一个给下游 sub agent 的任务合同，具体做法交给 task agent。
- 子任务必须只有一个 CLI 主目标；不能把多个主状态变化混在一起。
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
    prompt_variant: str = "claude-code",
) -> str:
    del rounds
    del next_step
    del prompt_variant
    role_instructions = CUA_HARNESS_CLI_TASK_INSTRUCTIONS
    task_package = extract_role_task_package(plan_text)
    return f"""\
{role_instructions.strip()}

本轮最小 CLI 子任务包:
{task_package.rstrip()}

执行要求:
- 只完成本轮最小子任务包里的 `任务:` 主目标。
- `边界:` 是硬边界；即使你认为能顺手推进全局任务，也不要越界修改、生成或提交其它最终产物。
- `相关已验证状态:` 是你可以复用的必要事实；没有写入这里的全局契约、原始题意或历史状态，不应作为执行依据。
- 不要重复生成 verifier 已确认完成的产物。
- 不要依赖被标记为 suspect/violation、伪造、不可信或已删除的产物。
- 如果当前子任务缺少必要上下文，停止并说明需要编排器在 `相关已验证状态` 或 `边界` 中补充什么，不要猜。
- 不要做另一类任务；如果需要另一类能力或更大的目标，停止并用自然语言说明需要重新拆解。
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
    prompt_variant: str = "claude-code",
) -> str:
    del next_step
    del prompt_variant
    role_name = "CLI"
    role_instructions = CUA_HARNESS_CLI_VERIFIER_INSTRUCTIONS
    blocker_and_risk_guidance = VERIFIER_BLOCKER_AND_RISK_GUIDANCE.strip()
    verifier_completion_guidance = DEFAULT_VERIFIER_CONTRACT_COMPLETION_GUIDANCE.strip()
    return f"""\
{role_instructions.strip()}

原始任务:
{task.rstrip()}

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
- `原题约束清单:` 从原始任务独立拆出会影响最终验收/评分的约束，至少覆盖最终消费者/状态载体、权威输入、候选选择、字段值、文件/路径、保存/提交/持久化边界、格式/精确文本/单位/精度、不能改变/不能遗漏/不能多做、非目标保持和禁止捷径。
- `契约覆盖检查:` 检查稳定任务契约中的 `验收约束` 是否漏掉、弱化、写歪或互相矛盾；如果契约没有显式 `验收约束` 段，也要从契约其它段落提取并指出缺口。
- `逐项反查:` 对每个验收约束写明: 约束内容、原题依据、是否 blocking、独立证据来源（原始任务/真实环境/权威输入/verifier 只读检查）、结论（`verified`、`unknown`、`violated` 或 `not_applicable`）。不能把“契约这么写”或 task agent 自述当作独立证据。
{blocker_and_risk_guidance}
- `过窄或错误解释:` 契约是否把自然语言任务解释得过窄、过严、过松，或排除了仍合理的候选解释。
- `建议契约修订:` 若契约需要修订，给出面向编排器的具体修订方向；若不需要，写“无”。
{verifier_completion_guidance}

请只审计当前子任务是否真实完成，是否保持本轮 {role_name} 主目标边界，是否存在完整性问题。
必须对照稳定任务契约和当前子任务合同，审计本轮结果是否仍服务于原始任务的真实最终状态，而不是只完成一个局部 proxy。
还必须审计权威输入闭包、状态载体、状态产生流程、提交/持久化边界和候选污染；如果本轮不涉及其中某项，也要在报告中说明“不适用”及理由。
请在报告中继续明确写出当前仍可信的中间产物/状态、当前新增可信产物、以及不可信或已删除产物。
必须包含 `给编排器的状态更新:` 段落。
输出自然语言审计报告，不要输出 JSON。
"""


def build_role_verifier_format_repair_prompt(
    *,
    report_text: str,
    prompt_variant: str = "claude-code",
) -> str:
    del prompt_variant
    return f"""\
你的上一份 verifier 报告缺少有效的前三行控制头。

这不是重新审计任务。不要使用工具，不要运行命令，不要修改、创建、移动或删除任何文件。
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
        if normalized in {"下一步:cli任务", "下一步：cli任务", "next:cli"}:
            return ORCHESTRATED_NEXT_CLI
        if normalized in {"下一步:完成", "下一步：完成", "next:done", "next:complete"}:
            return ORCHESTRATED_NEXT_DONE
        if normalized in {"下一步:阻塞", "下一步：阻塞", "next:blocked"}:
            return ORCHESTRATED_NEXT_BLOCKED
    return ORCHESTRATED_NEXT_INVALID


_ORCHESTRATOR_ROUTE_LINE_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*(?:下一步\s*[:：]\s*(?:CLI任务|完成|阻塞)|next\s*:\s*(?:cli|done|complete|blocked))"
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
        return raw[min(starts) :].strip()
    return raw[route_start:].strip()


def extract_role_task_package(text: str) -> str:
    """Return only the current route block intended for the task agent."""
    raw = str(text or "").strip()
    matches = list(_ORCHESTRATOR_ROUTE_LINE_RE.finditer(raw))
    if not matches:
        return raw
    return raw[matches[-1].start() :].strip()


_TASK_STATE_HEADER_RE = re.compile(r"(?im)^\s*(?:\*\*)?\s*当前任务状态\s*[:：]")
_TASK_STATE_BOUNDARY_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*(?:任务契约|任务语义合同|依赖判断|下一步\s*[:：]\s*(?:CLI任务|完成|阻塞)|next\s*:\s*(?:cli|done|complete|blocked))"
)
_TASK_CONTRACT_HEADER_RE = re.compile(r"(?im)^\s*(?:\*\*)?\s*(?:任务契约|任务语义合同)\s*[:：]")
_TASK_CONTRACT_BOUNDARY_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*(?:依赖判断|下一步\s*[:：]\s*(?:CLI任务|完成|阻塞)|next\s*:\s*(?:cli|done|complete|blocked))"
)
_ROUTE_LINE_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*(?:下一步\s*[:：]\s*(?:CLI任务|完成|阻塞)|next\s*:\s*(?:cli|done|complete|blocked))"
)
_RELATED_REPORT_SECTION_RE = re.compile(
    r"(?ims)^\s*(?:\*\*)?\s*相关(?:验证报告|已验证状态)\s*[:：]\s*(.*?)(?=^\s*(?:\*\*)?\s*(?:边界|任务|验收标准|下一步)\s*[:：]|\Z)"
)
_ROUND_REF_RE = re.compile(
    r"(?i)\bround_(\d+)\b"
)
_TRANSIENT_PRECONDITION_LINE_RE = re.compile(
    r"(?i)(?:依赖判断|前置|阻断当前动作|当前\s*gate|历史\s*gate|gate\s*清单|作为\s*gate)"
)


def _strip_transient_precondition_context(section: str) -> str:
    """Remove per-round dependency/gate material from stable cross-round state."""
    kept: list[str] = []
    for line in str(section or "").splitlines():
        if _TRANSIENT_PRECONDITION_LINE_RE.search(line):
            continue
        kept.append(line.rstrip())
    sanitized = "\n".join(kept).strip()
    return sanitized


def extract_role_task_state(text: str, *, fallback: str = "") -> str:
    raw = str(text or "").strip()
    match = _TASK_STATE_HEADER_RE.search(raw)
    if not match:
        return fallback.strip()
    boundary = _TASK_STATE_BOUNDARY_RE.search(raw, match.end())
    end = boundary.start() if boundary else len(raw)
    state = raw[match.start() : end].strip()
    state = _strip_transient_precondition_context(state)
    return state or fallback.strip()


def extract_role_task_contract(text: str, *, fallback: str = "") -> str:
    raw = str(text or "").strip()
    match = _TASK_CONTRACT_HEADER_RE.search(raw)
    if not match:
        return fallback.strip()
    boundary = _TASK_CONTRACT_BOUNDARY_RE.search(raw, match.end())
    end = boundary.start() if boundary else len(raw)
    contract = raw[match.start() : end].strip()
    contract = _strip_transient_precondition_context(contract)
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
