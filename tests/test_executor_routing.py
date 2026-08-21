from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from lh_harness.config import ProjectConfigError, create_project_config, load_run_defaults
from lh_harness.executor_routing import ExecutorRouter, executor_task_identity
import lh_harness.cli as cli
import lh_harness.manager as manager_runtime
from lh_harness.role_prompts import (
    MANAGER_NEXT_ASK,
    MANAGER_NEXT_BLOCKED,
    MANAGER_NEXT_CLI,
    MANAGER_NEXT_DONE,
    MANAGER_NEXT_GUI,
    build_role_manager_prompt,
    extract_related_report_refs,
    extract_role_manager_answer_choices,
    extract_role_manager_executor_tier,
    extract_role_manager_plan_text,
    extract_role_manager_question,
    extract_role_manager_task,
    extract_role_task_contract,
    extract_role_task_state,
    parse_role_manager_next_step,
)
from lh_harness.types import EpisodeResult, ExecutorRoutingConfig, HarnessConfig


def _cli_binding_args(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "agent": "codex",
        "model": "global-codex-model",
        "executor_agent": None,
        "executor_model": None,
        "gui_executor_agent": None,
        "gui_executor_model": None,
        "cli_executor_agent": None,
        "cli_executor_model": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _load(tmp_path: Path, content: str) -> dict[str, object]:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    return load_run_defaults(path)


def _tiered_config(
    *,
    default_tier: str = "cheap",
    escalation_tier: str = "strong",
    failures: str = "2",
    routing_extra: str = "",
    tier_extra: str = "",
) -> str:
    return f"""
[run.roles.executor]
agent = "codex"
model = "parent-model"

[run.roles.executor.cheap]
model = "cheap-model"
{tier_extra}

[run.roles.executor.strong]
agent = "deepseek_harness"

[run.executor_routing]
default_tier = "{default_tier}"
escalate_after_failures = {failures}
escalation_tier = "{escalation_tier}"
{routing_extra}
"""


def test_legacy_single_executor_and_generated_defaults_do_not_enable_routing(
    tmp_path: Path,
) -> None:
    legacy = _load(
        tmp_path,
        """
[run]
agent = "codex"

[run.roles.executor]
agent = "opencode"
model = "legacy-model"
""",
    )

    assert legacy["executor_agent"] == "opencode"
    assert legacy["executor_model"] == "legacy-model"
    assert "executor_tiers" not in legacy
    assert "executor_routing" not in legacy

    generated = tmp_path / "generated.toml"
    create_project_config(generated)
    generated_defaults = load_run_defaults(generated)
    assert "executor_tiers" not in generated_defaults
    assert "executor_routing" not in generated_defaults


def test_parent_executor_and_mixed_backend_tiers_are_preserved(tmp_path: Path) -> None:
    defaults = _load(tmp_path, _tiered_config())

    assert defaults["executor_agent"] == "codex"
    assert defaults["executor_model"] == "parent-model"
    assert defaults["executor_tiers"] == {
        "cheap": {"model": "cheap-model"},
        "strong": {"agent": "deepseek_harness"},
    }
    assert defaults["executor_routing"] == ExecutorRoutingConfig(
        default_tier="cheap",
        escalate_after_failures=2,
        escalation_tier="strong",
    )


@pytest.mark.parametrize(
    ("content", "message"),
    (
        (
            """
[run.roles.executor.cheap]
model = "cheap-model"
[run.roles.executor.strong]
model = "strong-model"
""",
            "run.executor_routing is required",
        ),
        (
            """
[run.executor_routing]
default_tier = "cheap"
escalate_after_failures = 2
escalation_tier = "strong"
""",
            "run.roles.executor must configure at least two tiers",
        ),
        (
            """
[run.roles.executor.cheap]
model = "cheap-model"
[run.executor_routing]
default_tier = "cheap"
escalate_after_failures = 2
escalation_tier = "strong"
""",
            "run.roles.executor must configure at least two tiers",
        ),
        (
            _tiered_config(escalation_tier="cheap"),
            "run.executor_routing.default_tier and run.executor_routing.escalation_tier",
        ),
        (
            _tiered_config(default_tier="missing"),
            "run.executor_routing.default_tier",
        ),
        (
            _tiered_config(escalation_tier="missing"),
            "run.executor_routing.escalation_tier",
        ),
    ),
)
def test_cross_table_routing_contract_is_strict(
    tmp_path: Path, content: str, message: str
) -> None:
    with pytest.raises(ProjectConfigError, match=message):
        _load(tmp_path, content)


@pytest.mark.parametrize(
    ("content", "message"),
    (
        (
            _tiered_config(routing_extra='typo = "value"'),
            r"unknown \[run.executor_routing\] key\(s\): typo",
        ),
        (
            _tiered_config(tier_extra='temperature = "low"'),
            r"unknown \[run.roles.executor.cheap\] key\(s\): temperature",
        ),
        (
            _tiered_config(tier_extra='agent = "unsupported"'),
            "run.roles.executor.cheap.agent",
        ),
        (
            """
[run.roles.executor]
cheap = "not-a-table"
strong = { model = "strong-model" }
[run.executor_routing]
default_tier = "cheap"
escalate_after_failures = 2
escalation_tier = "strong"
""",
            r"\[run.roles.executor.cheap\] must be a TOML table",
        ),
        (
            """
[run.roles.executor." bad"]
model = "cheap-model"
[run.roles.executor.strong]
model = "strong-model"
[run.executor_routing]
default_tier = " bad"
escalate_after_failures = 2
escalation_tier = "strong"
""",
            "run.roles.executor. bad",
        ),
        (
            """
[run.roles.executor.cheap]
model = ""
[run.roles.executor.strong]
model = "strong-model"
[run.executor_routing]
default_tier = "cheap"
escalate_after_failures = 2
escalation_tier = "strong"
""",
            "run.roles.executor.cheap.model",
        ),
        (_tiered_config(failures="true"), "run.executor_routing.escalate_after_failures"),
        (_tiered_config(failures="0"), "run.executor_routing.escalate_after_failures"),
    ),
)
def test_invalid_routing_values_report_their_toml_path(
    tmp_path: Path, content: str, message: str
) -> None:
    with pytest.raises(ProjectConfigError, match=message):
        _load(tmp_path, content)


def test_routing_table_requires_every_field(tmp_path: Path) -> None:
    with pytest.raises(
        ProjectConfigError,
        match=r"missing \[run.executor_routing\] key\(s\): escalation_tier",
    ):
        _load(
            tmp_path,
            """
[run.roles.executor.cheap]
model = "cheap-model"
[run.roles.executor.strong]
model = "strong-model"
[run.executor_routing]
default_tier = "cheap"
escalate_after_failures = 2
""",
        )


def test_harness_config_remains_backward_compatible_and_routing_is_frozen() -> None:
    legacy = HarnessConfig()
    assert legacy.executor_routing is None

    routing = ExecutorRoutingConfig("cheap", 2, "strong")
    configured = HarnessConfig(executor_routing=routing)
    assert configured.executor_routing is routing
    with pytest.raises(FrozenInstanceError):
        routing.default_tier = "strong"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    (
        {"default_tier": "", "escalate_after_failures": 1, "escalation_tier": "strong"},
        {"default_tier": " cheap", "escalate_after_failures": 1, "escalation_tier": "strong"},
        {"default_tier": "cheap", "escalate_after_failures": 1, "escalation_tier": "  "},
        {"default_tier": "cheap", "escalate_after_failures": 1, "escalation_tier": "cheap"},
        {"default_tier": "cheap", "escalate_after_failures": True, "escalation_tier": "strong"},
        {"default_tier": "cheap", "escalate_after_failures": 0, "escalation_tier": "strong"},
        {"default_tier": "cheap", "escalate_after_failures": 1.5, "escalation_tier": "strong"},
    ),
)
def test_executor_routing_dataclass_rejects_invalid_values(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        ExecutorRoutingConfig(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("language", ("en", "zh"))
def test_manager_prompt_omits_tier_policy_when_routing_is_disabled(language: str) -> None:
    prompt = build_role_manager_prompt(
        task="Complete the task.",
        rounds=[],
        round_index=1,
        language=language,
    )

    assert "Executor routing policy:" not in prompt
    assert "执行器路由策略:" not in prompt
    assert "Allowed executor tiers:" not in prompt
    assert "允许的执行器层级:" not in prompt
    assert "Executor tier:" not in prompt
    assert "执行器层级:" not in prompt


@pytest.mark.parametrize(
    ("language", "expected"),
    (
        (
            "en",
            (
                "Executor routing policy:",
                "Allowed executor tiers: cheap, strong",
                "Default tier when `Executor tier:` is omitted: cheap",
                "2 successfully parsed `Status: incomplete` or `Status: blocked` verdicts",
                "force tier strong",
                "naming strong uses it immediately",
                "Provider/runtime failures, timeouts, and format-repair failures do not count",
            ),
        ),
        (
            "zh",
            (
                "执行器路由策略:",
                "允许的执行器层级: cheap, strong",
                "省略 `执行器层级:` 时的默认层级: cheap",
                "2 次成功解析的 `状态: incomplete` 或 `状态: blocked` 结论",
                "强制使用层级 strong",
                "填写 strong 会立即使用该层级",
                "provider/runtime 失败、超时和格式修复失败不计数",
            ),
        ),
    ),
)
def test_manager_prompt_explains_enabled_routing_policy(
    language: str, expected: tuple[str, ...]
) -> None:
    prompt = build_role_manager_prompt(
        task="Complete the task.",
        rounds=[],
        round_index=1,
        executor_tiers={"strong": {"model": "strong"}, "cheap": {"model": "cheap"}},
        executor_routing=ExecutorRoutingConfig("cheap", 2, "strong"),
        language=language,
    )

    for marker in expected:
        assert marker in prompt


@pytest.mark.parametrize(
    ("plan", "expected"),
    (
        ("Next: cli\nExecutor tier: cheap\nTask: run tests", "cheap"),
        ("Next: gui\nExecutor tier: strong\nTask: inspect the screen", "strong"),
        ("下一步: CLI任务\n执行器层级: cheap\n任务: 运行测试", "cheap"),
        ("下一步: GUI任务\n执行器层级: strong\n任务: 检查屏幕", "strong"),
        ("Next: cli\nTask: run tests", None),
        ("Next: cli\nExecutor tier:\nTask: run tests", ""),
        ("Next: cli\nExecutor tier:", ""),
        ("Next: cli\nExecutor tier: experimental\nTask: run tests", "experimental"),
        ("Next: done\nExecutor tier: strong", None),
        ("Next: ask\nExecutor tier: strong\nQuestion: proceed?", None),
        ("Next: blocked\nExecutor tier: strong", None),
    ),
)
def test_executor_tier_extraction_is_route_scoped(
    plan: str, expected: str | None
) -> None:
    assert extract_role_manager_executor_tier(plan) == expected


def test_executor_tier_ignores_state_contract_and_task_body_text() -> None:
    plan = """
Current task state:
Executor tier: stale-state-value
Task contract:
Executor tier: stale-contract-value
Dependency assessment: route the actual CLI task
Next: cli
Task: Write documentation containing this literal line:
Executor tier: example-only
Boundaries: documentation only
"""

    assert extract_role_manager_executor_tier(plan) is None
    assert "Executor tier: example-only" in extract_role_manager_task(plan)


@pytest.mark.parametrize(
    ("plan", "expected_tier", "expected_task"),
    (
        (
            """
Current task state:
The documentation quotes this obsolete protocol:
Next: gui
Executor tier: cheap
Task: stale state example
Task contract:
It also quotes another obsolete protocol:
Next: gui
Executor tier: cheap
Task: stale contract example
Dependency assessment: route the real CLI work
Next: cli
Executor tier: strong
Task: actual task
Boundaries: tests only
""",
            "strong",
            "actual task",
        ),
        (
            """
当前任务状态:
说明中引用了过期协议:
下一步: GUI任务
执行器层级: cheap
任务: 状态中的旧示例
任务契约:
这里也引用另一个过期协议:
下一步: GUI任务
执行器层级: cheap
任务: 契约中的旧示例
依赖判断: 路由真实 CLI 工作
下一步: CLI任务
执行器层级: strong
任务: 真实任务
边界: 只运行测试
""",
            "strong",
            "真实任务",
        ),
    ),
)
def test_full_routes_in_state_and_contract_do_not_gain_protocol_authority(
    plan: str, expected_tier: str, expected_task: str
) -> None:
    extracted = extract_role_manager_plan_text(plan)
    assert parse_role_manager_next_step(extracted) == MANAGER_NEXT_CLI
    assert extract_role_manager_executor_tier(extracted) == expected_tier
    assert extract_role_manager_task(extracted) == expected_task


def test_executor_tier_and_task_use_only_the_final_route_block() -> None:
    plan = """
Next: cli
Executor tier: cheap
Task: obsolete task
Boundaries: old

---
Current task state: beginning a new manager block
Task contract: preserve the final visible state
Dependency assessment: GUI verification is next
Next: gui
Executor tier: strong
Task: inspect final state
Acceptance criteria: visible state is correct
"""

    assert extract_role_manager_executor_tier(plan) == "strong"
    assert extract_role_manager_task(plan) == "inspect final state"


@pytest.mark.parametrize(
    ("route", "tier", "task", "section", "done", "ask", "nested_route", "nested_tier", "nested_task"),
    (
        (
            "Next: cli",
            "Executor tier: strong",
            "Task: update docs",
            "Acceptance criteria",
            "Next: done",
            "Next: ask",
            "Next: gui",
            "Executor tier: cheap",
            "Task: nested example",
        ),
        (
            "Next: cli",
            "Executor tier: strong",
            "Task: update docs",
            "Related audit reports",
            "Next: done",
            "Next: ask",
            "Next: gui",
            "Executor tier: cheap",
            "Task: nested example",
        ),
        (
            "Next: cli",
            "Executor tier: strong",
            "Task: update docs",
            "Related audited state",
            "Next: done",
            "Next: ask",
            "Next: gui",
            "Executor tier: cheap",
            "Task: nested example",
        ),
        (
            "Next: cli",
            "Executor tier: strong",
            "Task: update docs",
            "Boundaries",
            "Next: done",
            "Next: ask",
            "Next: gui",
            "Executor tier: cheap",
            "Task: nested example",
        ),
        (
            "下一步: CLI任务",
            "执行器层级: strong",
            "任务: 更新文档",
            "验收标准",
            "下一步: 完成",
            "下一步: 请示用户",
            "下一步: GUI任务",
            "执行器层级: cheap",
            "任务: 嵌套示例",
        ),
        (
            "下一步: CLI任务",
            "执行器层级: strong",
            "任务: 更新文档",
            "相关审计报告",
            "下一步: 完成",
            "下一步: 请示用户",
            "下一步: GUI任务",
            "执行器层级: cheap",
            "任务: 嵌套示例",
        ),
        (
            "下一步: CLI任务",
            "执行器层级: strong",
            "任务: 更新文档",
            "相关已审计状态",
            "下一步: 完成",
            "下一步: 请示用户",
            "下一步: GUI任务",
            "执行器层级: cheap",
            "任务: 嵌套示例",
        ),
        (
            "下一步: CLI任务",
            "执行器层级: strong",
            "任务: 更新文档",
            "边界",
            "下一步: 完成",
            "下一步: 请示用户",
            "下一步: GUI任务",
            "执行器层级: cheap",
            "任务: 嵌套示例",
        ),
    ),
)
def test_all_subtask_sections_lock_route_literals_and_nested_protocols(
    route: str,
    tier: str,
    task: str,
    section: str,
    done: str,
    ask: str,
    nested_route: str,
    nested_tier: str,
    nested_task: str,
) -> None:
    plan = "\n".join(
        (
            route,
            tier,
            task,
            f"{section}: literal examples follow",
            done,
            ask,
            nested_route,
            nested_tier,
            nested_task,
        )
    )

    extracted = extract_role_manager_plan_text(plan)
    assert extract_role_manager_executor_tier(extracted) == "strong"
    assert extract_role_manager_task(extracted) in {"update docs", "更新文档"}
    assert extracted.startswith(route)


@pytest.mark.parametrize(
    ("language", "section"),
    (
        ("en", "Acceptance criteria"),
        ("en", "Related audit reports"),
        ("en", "Boundaries"),
        ("zh", "验收标准"),
        ("zh", "相关审计报告"),
        ("zh", "边界"),
    ),
)
def test_incomplete_manager_block_inside_payload_cannot_replace_authority(
    language: str, section: str
) -> None:
    if language == "en":
        plan = f"""
Next: cli
Executor tier: strong
Task: update docs
{section}: literal manager excerpt follows
Current task state: quoted, not a new complete block
Dependency assessment: quoted without a Task contract
Next: gui
Executor tier: cheap
Task: nested literal
"""
        expected_task = "update docs"
    else:
        plan = f"""
下一步: CLI任务
执行器层级: strong
任务: 更新文档
{section}: 后面是字面 manager 摘录
当前任务状态: 引用文本，不是新的完整块
依赖判断: 引用中缺少任务契约
下一步: GUI任务
执行器层级: cheap
任务: 嵌套字面示例
"""
        expected_task = "更新文档"

    extracted = extract_role_manager_plan_text(plan)
    assert parse_role_manager_next_step(extracted) == MANAGER_NEXT_CLI
    assert extract_role_manager_executor_tier(extracted) == "strong"
    assert extract_role_manager_task(extracted) == expected_task


@pytest.mark.parametrize(
    ("plan", "expected_prefix", "expected_tier", "expected_task", "obsolete"),
    (
        (
            """
Next: cli
Executor tier: strong
Task: obsolete task
Boundaries: documentation only
Next: done
Next: gui
Executor tier: cheap
Task: nested boundary example
Current task state: new transcript block
Task contract: inspect the visible result
Dependency assessment: GUI verification is ready
Next: gui
Executor tier: cheap
Task: actual final task
Boundaries: inspect only
""",
            "Current task state:",
            "cheap",
            "actual final task",
            "obsolete task",
        ),
        (
            """
下一步: CLI任务
执行器层级: strong
任务: 旧任务
边界: 只处理文档
下一步: 完成
下一步: GUI任务
执行器层级: cheap
任务: 边界中的嵌套示例
当前任务状态: 新的完整协议块
任务契约: 检查可见结果
依赖判断: GUI 验证已就绪
下一步: GUI任务
执行器层级: cheap
任务: 最终真实任务
边界: 只检查
""",
            "当前任务状态:",
            "cheap",
            "最终真实任务",
            "旧任务",
        ),
    ),
)
def test_explicit_new_manager_block_can_follow_a_locked_payload(
    plan: str,
    expected_prefix: str,
    expected_tier: str,
    expected_task: str,
    obsolete: str,
) -> None:
    extracted = extract_role_manager_plan_text(plan)
    assert extracted.startswith(expected_prefix)
    assert obsolete not in extracted
    assert extract_role_manager_executor_tier(extracted) == expected_tier
    assert extract_role_manager_task(extracted) == expected_task


def test_initial_legacy_contract_block_is_preserved_without_current_state() -> None:
    plan = """
wrapper prose that is not part of the manager protocol
Task contract: preserve the requested artifact
Dependency assessment: CLI work is ready
Next: cli
Executor tier: strong
Task: run the final tests
Boundaries: tests only
"""

    extracted = extract_role_manager_plan_text(plan)
    assert extracted.startswith("Task contract: preserve the requested artifact")
    assert parse_role_manager_next_step(extracted) == MANAGER_NEXT_CLI
    assert extract_role_manager_executor_tier(extracted) == "strong"
    assert extract_role_manager_task(extracted) == "run the final tests"


@pytest.mark.parametrize(
    ("plan", "expected_route", "expected_state", "expected_contract"),
    (
        (
            """Next: cli
Current task state:
CLI work remains.
Task contract:
Preserve the requested behavior.
""",
            MANAGER_NEXT_CLI,
            "CLI work remains.",
            "Preserve the requested behavior.",
        ),
        (
            """Next: gui
Current task state:
Visual verification remains.
""",
            MANAGER_NEXT_GUI,
            "Visual verification remains.",
            "prior contract",
        ),
        (
            """下一步: CLI任务
当前任务状态:
仍需执行 CLI 工作。
任务契约:
保留请求的行为。
""",
            MANAGER_NEXT_CLI,
            "仍需执行 CLI 工作。",
            "保留请求的行为。",
        ),
        (
            """下一步: GUI任务
当前任务状态:
仍需视觉验证。
""",
            MANAGER_NEXT_GUI,
            "仍需视觉验证。",
            "prior contract",
        ),
    ),
)
def test_legacy_route_first_short_plan_restores_state_and_optional_contract(
    plan: str,
    expected_route: str,
    expected_state: str,
    expected_contract: str,
) -> None:
    extracted = extract_role_manager_plan_text(plan)
    assert parse_role_manager_next_step(extracted) == expected_route
    assert expected_state in extract_role_task_state(extracted)
    assert extract_role_task_contract(extracted, fallback="prior contract").endswith(
        expected_contract
    )
    assert extract_role_manager_task(extracted) == ""


@pytest.mark.parametrize(
    ("plan", "expected_state"),
    (
        ("Next: done\nCurrent task state:\nAll work is verified.\n", "All work is verified."),
        ("下一步: 完成\n当前任务状态:\n全部工作已经验证。\n", "全部工作已经验证。"),
    ),
)
def test_terminal_route_first_short_plan_restores_state(
    plan: str, expected_state: str
) -> None:
    assert parse_role_manager_next_step(plan) == MANAGER_NEXT_DONE
    assert expected_state in extract_role_task_state(plan)


def test_route_first_state_payload_cannot_replace_top_level_authority() -> None:
    plan = """Next: cli
Current task state:
The retained state quotes a non-authoritative payload route:
Next: gui
Executor tier: strong
Task: quoted payload only
"""

    assert parse_role_manager_next_step(plan) == MANAGER_NEXT_CLI
    assert extract_role_manager_executor_tier(plan) is None
    assert "Next: gui" in extract_role_task_state(plan)


def test_enabled_routing_gives_legacy_missing_tasks_per_round_identities() -> None:
    router = _router(threshold=1)
    plan = "Next: cli\nCurrent task state:\nCLI work remains.\n"
    next_step = parse_role_manager_next_step(plan)
    task = extract_role_manager_task(plan)

    first = router.select(
        next_step=next_step,
        task_text=task,
        requested_tier=extract_role_manager_executor_tier(plan),
        round_index=1,
    )
    router.observe(first, "blocked")
    second = router.select(
        next_step=next_step,
        task_text=task,
        requested_tier=extract_role_manager_executor_tier(plan),
        round_index=2,
    )

    assert next_step == MANAGER_NEXT_CLI
    assert task == ""
    assert first.task_present is False
    assert second.task_present is False
    assert first.task_id != second.task_id
    assert second.failures_before == 0
    assert second.selected_tier == "cheap"


@pytest.mark.parametrize("language", ("en", "zh"))
@pytest.mark.parametrize(
    "expected_route",
    (
        MANAGER_NEXT_GUI,
        MANAGER_NEXT_CLI,
        MANAGER_NEXT_ASK,
        MANAGER_NEXT_DONE,
        MANAGER_NEXT_BLOCKED,
    ),
)
def test_all_manager_fields_share_final_structural_authority(
    language: str, expected_route: str
) -> None:
    if language == "en":
        prefix = """noisy transcript wrapper
Current task state:
Verified state includes literal protocol examples:
Next: gui
Question: fake state question
Choices: wrong | worse
Related audit reports: round_001
state tail survives
Task contract:
Keep this literal terminal route in the contract:
Next: blocked
Question: fake contract question
Choices: bad | worse
Related audit reports: round_001
contract tail survives
Dependency assessment:
This dependency prose mentions a non-protocol route:
Next: gui
dependency tail survives
"""
        if expected_route in {MANAGER_NEXT_GUI, MANAGER_NEXT_CLI}:
            route_line = "Next: gui" if expected_route == MANAGER_NEXT_GUI else "Next: cli"
            tail = f"""{route_line}
Executor tier: strong
Task: actual executable task
Acceptance criteria: preserve these literal examples
Next: done
Question: fake acceptance question
Choices: wrong | worse
Next: gui
Executor tier: cheap
Task: nested acceptance example
Related audit reports: round_003 for the actual dependency
Related audited state: round_003 remains relevant
Boundaries: literal nested route below is not authoritative
Next: blocked
"""
        elif expected_route == MANAGER_NEXT_ASK:
            tail = """Next: ask
Question: proceed with the actual choice?
Choices: yes | no
Related audit reports: round_003 must not attach to a terminal route
"""
        elif expected_route == MANAGER_NEXT_DONE:
            tail = """Next: done
Reason: all verified requirements passed
Related audit reports: round_003 must not attach to a terminal route
"""
        else:
            tail = """Next: blocked
Reason: an external prerequisite is unavailable
Related audit reports: round_003 must not attach to a terminal route
"""
        expected_state_tail = "state tail survives"
        expected_contract_tail = "contract tail survives"
        expected_question = "proceed with the actual choice?"
        expected_choices = ["yes", "no"]
        expected_task = "actual executable task"
    else:
        prefix = """嘈杂的 transcript 包装
当前任务状态:
已验证状态包含以下字面协议示例:
下一步: GUI任务
问题: 状态中的假问题
选项: 错误 | 更差
相关审计报告: round_001
状态尾部必须保留
任务契约:
契约中保留这个字面终止路由:
下一步: 阻塞
问题: 契约中的假问题
选项: 不好 | 更差
相关审计报告: round_001
契约尾部必须保留
依赖判断:
依赖正文提到一个非协议路由:
下一步: GUI任务
依赖尾部必须保留
"""
        if expected_route in {MANAGER_NEXT_GUI, MANAGER_NEXT_CLI}:
            route_line = "下一步: GUI任务" if expected_route == MANAGER_NEXT_GUI else "下一步: CLI任务"
            tail = f"""{route_line}
执行器层级: strong
任务: 真实可执行任务
验收标准: 保留下列字面示例
下一步: 完成
问题: 验收段中的假问题
选项: 错误 | 更差
下一步: GUI任务
执行器层级: cheap
任务: 验收段中的嵌套示例
相关审计报告: round_003 对应真实依赖
相关已审计状态: round_003 仍然相关
边界: 下方字面嵌套路由不具备权威性
下一步: 阻塞
"""
        elif expected_route == MANAGER_NEXT_ASK:
            tail = """下一步: 请示用户
问题: 是否继续真实选择？
选项: 是 | 否
相关审计报告: round_003 不应附加到终止路由
"""
        elif expected_route == MANAGER_NEXT_DONE:
            tail = """下一步: 完成
原因: 已验证全部要求
相关审计报告: round_003 不应附加到终止路由
"""
        else:
            tail = """下一步: 阻塞
原因: 外部前置条件不可用
相关审计报告: round_003 不应附加到终止路由
"""
        expected_state_tail = "状态尾部必须保留"
        expected_contract_tail = "契约尾部必须保留"
        expected_question = "是否继续真实选择？"
        expected_choices = ["是", "否"]
        expected_task = "真实可执行任务"

    plan = prefix + tail
    extracted = extract_role_manager_plan_text(plan)

    assert extracted.startswith("Current task state:" if language == "en" else "当前任务状态:")
    assert parse_role_manager_next_step(extracted) == expected_route
    assert expected_state_tail in extract_role_task_state(extracted)
    assert "Next: gui" in extract_role_task_state(extracted) or "下一步: GUI任务" in extract_role_task_state(extracted)
    assert expected_contract_tail in extract_role_task_contract(extracted)
    assert "round_001" in extract_role_task_contract(extracted)

    from lh_harness.dashboard.state import _infer_next_step

    assert _infer_next_step(extracted) == expected_route
    if expected_route in {MANAGER_NEXT_GUI, MANAGER_NEXT_CLI}:
        assert extract_role_manager_executor_tier(extracted) == "strong"
        assert extract_role_manager_task(extracted) == expected_task
        assert extract_related_report_refs(extracted) == ["round_003"]
        assert extract_role_manager_question(extracted) == ""
        assert extract_role_manager_answer_choices(extracted) == []
    elif expected_route == MANAGER_NEXT_ASK:
        assert extract_role_manager_executor_tier(extracted) is None
        assert extract_role_manager_task(extracted) == ""
        assert extract_role_manager_question(extracted) == expected_question
        assert extract_role_manager_answer_choices(extracted) == expected_choices
        assert extract_related_report_refs(extracted) == []
    else:
        assert extract_role_manager_executor_tier(extracted) is None
        assert extract_role_manager_task(extracted) == ""
        assert extract_role_manager_question(extracted) == ""
        assert extract_role_manager_answer_choices(extracted) == []
        assert extract_related_report_refs(extracted) == []


@pytest.mark.parametrize("language", ("en", "zh"))
def test_related_refs_require_authoritative_executable_section(language: str) -> None:
    plan = (
        """Current task state:
Related audit reports: round_001 is only state prose
Task contract: preserve the result
Dependency assessment: CLI work is ready
Next: cli
Task: execute without related context
Boundaries: tests only
"""
        if language == "en"
        else """当前任务状态:
相关审计报告: round_001 只是状态正文
任务契约: 保留结果
依赖判断: CLI 工作已就绪
下一步: CLI任务
任务: 不带相关上下文执行
边界: 只运行测试
"""
    )
    assert extract_related_report_refs(plan) == []


@pytest.mark.parametrize(
    ("plan", "expected_question", "expected_choices"),
    (
        (
            """Current task state:
Choices: wrong | worse
Task contract: preserve the actual question
Dependency assessment: ask the user
Next: ask
Question: whether to continue yes/no?
""",
            "whether to continue yes/no?",
            ["Yes", "No"],
        ),
        (
            """当前任务状态:
选项: 错误 | 更差
任务契约: 保留真实问题
依赖判断: 请示用户
下一步: 请示用户
问题: 是否继续？
""",
            "是否继续？",
            ["是", "否"],
        ),
    ),
)
def test_yes_no_fallback_uses_only_authoritative_question(
    plan: str, expected_question: str, expected_choices: list[str]
) -> None:
    assert extract_role_manager_question(plan) == expected_question
    assert extract_role_manager_answer_choices(plan) == expected_choices


def test_legacy_contract_without_dependency_keeps_contract_and_route() -> None:
    plan = """Task contract: legacy contract without dependency
Next: cli
Task: run legacy task
Boundaries: task only
"""
    extracted = extract_role_manager_plan_text(plan)
    assert extracted.startswith("Task contract: legacy contract without dependency")
    assert parse_role_manager_next_step(extracted) == MANAGER_NEXT_CLI
    assert "legacy contract without dependency" in extract_role_task_contract(extracted)


@pytest.mark.parametrize(
    ("plan", "expected_tier", "expected_task"),
    (
        (
            """
Next: cli
Executor tier: cheap
Task:
Document this literal protocol example:
Next: cli
This line is quoted task content, not a new route block.
Boundaries: documentation only
""",
            "cheap",
            "Document this literal protocol example:\nNext: cli\nThis line is quoted task content, not a new route block.",
        ),
        (
            """
下一步: GUI任务
执行器层级: strong
任务:
在说明中保留下列字面示例:
下一步: GUI任务
这只是任务正文，不是新路由块。
边界: 只核对说明
""",
            "strong",
            "在说明中保留下列字面示例:\n下一步: GUI任务\n这只是任务正文，不是新路由块。",
        ),
    ),
)
def test_route_like_task_body_lines_do_not_replace_the_outer_route(
    plan: str, expected_tier: str, expected_task: str
) -> None:
    assert extract_role_manager_executor_tier(plan) == expected_tier
    assert extract_role_manager_task(plan) == expected_task


@pytest.mark.parametrize(
    ("plan", "expected_tier", "task_markers"),
    (
        (
            """
Next: cli
Executor tier: cheap
Task:
Document these literal route examples:
Next: done
Next: ask
Next: blocked
Next: gui
Executor tier: strong
Task: nested example only
The nested block is still documentation payload.
Boundaries: documentation only
""",
            "cheap",
            ("Next: done", "Next: ask", "Next: blocked", "Task: nested example only"),
        ),
        (
            """
下一步: GUI任务
执行器层级: strong
任务:
保留下列字面路由示例:
下一步: 完成
下一步: 请示用户
下一步: 阻塞
下一步: CLI任务
执行器层级: cheap
任务: 仅为嵌套示例
嵌套块仍属于任务正文。
边界: 只处理说明文字
""",
            "strong",
            ("下一步: 完成", "下一步: 请示用户", "下一步: 阻塞", "任务: 仅为嵌套示例"),
        ),
    ),
)
def test_task_payload_locks_all_nested_route_protocols_until_a_boundary(
    plan: str, expected_tier: str, task_markers: tuple[str, ...]
) -> None:
    assert extract_role_manager_executor_tier(plan) == expected_tier
    task = extract_role_manager_task(plan)
    for marker in task_markers:
        assert marker in task


@pytest.mark.parametrize(
    ("plan", "expected"),
    (
        ("Next: cli\nTask: run the focused tests", "run the focused tests"),
        (
            "Next: gui\nTask:\nOpen the settings page.\nVerify the saved value.\nBoundaries: no other changes",
            "Open the settings page.\nVerify the saved value.",
        ),
        ("下一步: CLI任务\n任务: 运行聚焦测试\n验收标准: 全部通过", "运行聚焦测试"),
        (
            "下一步: GUI任务\n执行器层级: strong\n任务:\n打开设置页。\n核对保存值。\n相关审计报告: round_001",
            "打开设置页。\n核对保存值。",
        ),
        ("Next: cli\nExecutor tier: cheap\nBoundaries: tests only", ""),
        ("Next: done\nTask: should not be executable", ""),
    ),
)
def test_manager_task_extraction_is_narrow_and_bounded(plan: str, expected: str) -> None:
    assert extract_role_manager_task(plan) == expected


def _router(*, threshold: int = 2) -> ExecutorRouter:
    return ExecutorRouter(
        {"cheap", "strong"},
        ExecutorRoutingConfig("cheap", threshold, "strong"),
    )


@pytest.mark.parametrize("tier", (" premium ", "premium!", "x" * 65))
def test_router_rejects_noncanonical_programmatic_tier_names(tier: str) -> None:
    with pytest.raises(ValueError, match="1-64 ASCII"):
        ExecutorRouter(
            {"cheap", "strong", tier},
            ExecutorRoutingConfig("cheap", 2, "strong"),
        )


def test_router_default_and_explicit_escalation_selection() -> None:
    router = _router()
    default = router.select(
        next_step=MANAGER_NEXT_CLI,
        task_text="run focused tests",
        requested_tier=None,
        round_index=1,
    )
    strong = router.select(
        next_step=MANAGER_NEXT_GUI,
        task_text="inspect the screen",
        requested_tier="strong",
        round_index=2,
    )

    assert (default.selected_tier, default.selection_reason) == ("cheap", "default")
    assert (strong.selected_tier, strong.selection_reason) == (
        "strong",
        "manager_escalation",
    )
    assert strong.escalation_active is True


def test_router_explicit_escalation_is_sticky_against_a_downgrade_request() -> None:
    router = _router()
    strong = router.select(
        next_step=MANAGER_NEXT_CLI,
        task_text="same task",
        requested_tier="strong",
        round_index=1,
    )
    retry = router.select(
        next_step=MANAGER_NEXT_CLI,
        task_text="same task",
        requested_tier="cheap",
        round_index=2,
    )

    assert strong.selected_tier == "strong"
    assert retry.selected_tier == "strong"
    assert retry.selection_reason == "automatic_escalation"


def test_router_escalates_on_the_run_after_the_threshold_and_stays_sticky() -> None:
    router = _router(threshold=2)
    first = router.select(
        next_step=MANAGER_NEXT_CLI,
        task_text="same task",
        requested_tier=None,
        round_index=1,
    )
    first = router.observe(first, "incomplete")
    second = router.select(
        next_step=MANAGER_NEXT_CLI,
        task_text="  same   task ",
        requested_tier="cheap",
        round_index=2,
    )
    second = router.observe(second, "blocked")
    third = router.select(
        next_step=MANAGER_NEXT_CLI,
        task_text="same task",
        requested_tier="cheap",
        round_index=3,
    )

    assert first.failures_after == 1
    assert second.selected_tier == "cheap"
    assert second.failures_after == 2
    assert second.escalation_active is True
    assert third.selected_tier == "strong"
    assert third.selection_reason == "automatic_escalation"
    assert third.failures_before == 2


def test_router_new_task_route_change_and_pass_reset_to_default() -> None:
    router = _router(threshold=1)
    cli = router.select(
        next_step=MANAGER_NEXT_CLI,
        task_text="same words",
        requested_tier=None,
        round_index=1,
    )
    router.observe(cli, "incomplete")
    gui = router.select(
        next_step=MANAGER_NEXT_GUI,
        task_text="same words",
        requested_tier=None,
        round_index=2,
    )
    completed = router.observe(gui, "complete")
    next_task = router.select(
        next_step=MANAGER_NEXT_GUI,
        task_text="same words",
        requested_tier=None,
        round_index=3,
    )

    assert gui.selected_tier == "cheap"
    assert gui.failures_before == 0
    assert completed.audit_signal == "complete"
    assert completed.failures_after == 0
    assert next_task.selected_tier == "cheap"
    assert next_task.failures_before == 0


def test_router_invalid_tier_is_atomic_and_does_not_reset_active_task() -> None:
    router = _router(threshold=2)
    first = router.select(
        next_step=MANAGER_NEXT_CLI,
        task_text="active task",
        requested_tier=None,
        round_index=1,
    )
    router.observe(first, "incomplete")
    rejected = router.select(
        next_step=MANAGER_NEXT_GUI,
        task_text="different task",
        requested_tier="unknown",
        round_index=2,
    )
    resumed = router.select(
        next_step=MANAGER_NEXT_CLI,
        task_text="active task",
        requested_tier=None,
        round_index=3,
    )

    assert rejected.selection_reason == "invalid"
    assert rejected.selected_tier is None
    assert resumed.failures_before == 1


def test_router_empty_tasks_never_accumulate_across_rounds() -> None:
    router = _router(threshold=1)
    first = router.select(
        next_step=MANAGER_NEXT_CLI,
        task_text="",
        requested_tier=None,
        round_index=1,
    )
    router.observe(first, "blocked")
    second = router.select(
        next_step=MANAGER_NEXT_CLI,
        task_text="   ",
        requested_tier=None,
        round_index=2,
    )

    assert first.task_present is False
    assert second.task_present is False
    assert first.task_id != second.task_id
    assert second.failures_before == 0
    assert second.selected_tier == "cheap"


def test_router_not_counted_signal_preserves_failures_without_clearing() -> None:
    router = _router(threshold=2)
    first = router.select(
        next_step=MANAGER_NEXT_CLI,
        task_text="active task",
        requested_tier=None,
        round_index=1,
    )
    router.observe(first, "incomplete")
    timeout = router.select(
        next_step=MANAGER_NEXT_CLI,
        task_text="active task",
        requested_tier=None,
        round_index=2,
    )
    timeout = router.observe(timeout, "not_counted")
    retry = router.select(
        next_step=MANAGER_NEXT_CLI,
        task_text="active task",
        requested_tier=None,
        round_index=3,
    )

    assert timeout.audit_signal == "not_counted"
    assert timeout.failures_after == 1
    assert retry.failures_before == 1
    assert retry.selected_tier == "cheap"


def test_task_identity_normalizes_whitespace_but_includes_route() -> None:
    first, _ = executor_task_identity(
        next_step=MANAGER_NEXT_CLI, task_text="run\n focused   tests", round_index=1
    )
    normalized, _ = executor_task_identity(
        next_step=MANAGER_NEXT_CLI, task_text="run focused tests", round_index=9
    )
    gui, _ = executor_task_identity(
        next_step=MANAGER_NEXT_GUI, task_text="run focused tests", round_index=1
    )

    assert first == normalized
    assert first != gui


class _QueueAgent:
    def __init__(
        self,
        name: str,
        outputs: list[str | EpisodeResult],
        calls: list[str] | None = None,
    ) -> None:
        self.name = name
        self.outputs = list(outputs)
        self.prompts: list[str] = []
        self.calls = calls

    async def run_episode(self, prompt, _env, _budget, live_trajectory_path=None):
        self.prompts.append(prompt)
        if self.calls is not None:
            self.calls.append(self.name)
        output = self.outputs.pop(0)
        if isinstance(output, EpisodeResult):
            return output
        return EpisodeResult(status="done", actions_log=output)


def _patch_kernel_boundaries(monkeypatch: pytest.MonkeyPatch):
    events: list[tuple[str, dict[str, object]]] = []
    records: list[object] = []

    async def noop_async(*_args, **_kwargs):
        return None

    async def record_round(_env, _config, _role_dir, _events_path, record):
        records.append(record)

    async def no_gate(*_args, **_kwargs):
        return False

    monkeypatch.setattr(manager_runtime, "_ensure_dir_nofollow", lambda *_a, **_k: None)
    monkeypatch.setattr(manager_runtime, "_ensure_remote_layout", noop_async)
    monkeypatch.setattr(manager_runtime, "_write_remote_round_text", noop_async)
    monkeypatch.setattr(manager_runtime, "_write_remote_text", noop_async)
    monkeypatch.setattr(manager_runtime, "_write_local", lambda *_a, **_k: None)
    monkeypatch.setattr(manager_runtime, "_save_role_result", lambda *_a, **_k: {})
    monkeypatch.setattr(manager_runtime, "_capture_environment_screenshot", noop_async)
    monkeypatch.setattr(manager_runtime, "_merge_episode_logs", lambda *_a, **_k: None)
    monkeypatch.setattr(manager_runtime, "_record_round", record_round)
    monkeypatch.setattr(manager_runtime, "_human_gate", no_gate)
    monkeypatch.setattr(
        manager_runtime,
        "_append_event",
        lambda _path, event, payload: events.append((event, payload)),
    )
    return events, records


def _routing_config(tmp_path: Path, *, rounds: int) -> HarnessConfig:
    return HarnessConfig(
        max_total_episodes=rounds,
        workspace_path=str(tmp_path / "workspace"),
        harness_dir=str(tmp_path / "harness"),
        log_dir=str(tmp_path / "logs"),
        executor_routing=ExecutorRoutingConfig("cheap", 2, "strong"),
    )


def _audit(status: str) -> str:
    return (
        f"Status: {status}\n"
        "Integrity: clean\n"
        "Contract audit: aligned\n"
        "Acceptance-constraint backcheck: no blocking constraints\n"
        "State update for manager: verified"
    )


def test_kernel_uses_one_authority_for_route_tier_and_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events, records = _patch_kernel_boundaries(monkeypatch)
    calls: list[str] = []
    manager = _QueueAgent(
        "manager",
        [
            """
Current task state:
The documentation quotes an obsolete route:
Next: gui
Executor tier: cheap
Task: stale state example
Task contract:
Preserve the real CLI deliverable; this is another quoted route:
Next: gui
Executor tier: cheap
Task: stale contract example
Dependency assessment: the real CLI work is ready
Next: cli
Executor tier: strong
Task: actual CLI task
Boundaries: tests only
"""
        ],
    )
    auditor = _QueueAgent("auditor", [_audit("complete")])
    gui_cheap = _QueueAgent("gui-cheap", [], calls)
    gui_strong = _QueueAgent("gui-strong", [], calls)
    cli_cheap = _QueueAgent("cli-cheap", [], calls)
    cli_strong = _QueueAgent("cli-strong", ["done"], calls)

    asyncio.run(
        manager_runtime._run_impl(
            task="unified parser authority",
            env=object(),
            config=_routing_config(tmp_path, rounds=1),
            manager_agent=manager,
            gui_executor_agents={"cheap": gui_cheap, "strong": gui_strong},
            cli_executor_agents={"cheap": cli_cheap, "strong": cli_strong},
            gui_auditor_agent=auditor,
            cli_auditor_agent=auditor,
        )
    )

    assert calls == ["cli-strong"]
    assert records[0].next_step == MANAGER_NEXT_CLI
    assert records[0].executor_routing["selected_tier"] == "strong"
    assert records[0].executor_routing["task_present"] is True
    manager_done = next(
        payload for event, payload in events if event == "manager_round_done"
    )
    assert manager_done["next_step"] == MANAGER_NEXT_CLI
    assert manager_done["executor_routing"]["selected_tier"] == "strong"


def test_kernel_ask_gate_uses_authoritative_question_and_choices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _events, records = _patch_kernel_boundaries(monkeypatch)
    captured: list[dict[str, object]] = []

    async def capture_gate(
        _gate,
        reason,
        round_index,
        _task_state,
        **kwargs,
    ) -> bool:
        captured.append({"reason": reason, "round": round_index, **kwargs})
        return True

    monkeypatch.setattr(manager_runtime, "_human_gate", capture_gate)
    manager = _QueueAgent(
        "manager",
        [
            """
Current task state:
Question: fake state question
Choices: wrong | worse
Task contract:
Question: fake contract question
Choices: bad | worse
Dependency assessment: a human decision is required
Next: ask
Question: proceed with the actual operation?
Choices: approve | reject
"""
        ],
    )
    unused = _QueueAgent("unused", [])

    asyncio.run(
        manager_runtime._run_impl(
            task="ask with one authority",
            env=object(),
            config=_routing_config(tmp_path, rounds=1),
            manager_agent=manager,
            gui_executor_agents={"cheap": unused, "strong": unused},
            cli_executor_agents={"cheap": unused, "strong": unused},
            gui_auditor_agent=unused,
            cli_auditor_agent=unused,
        )
    )

    assert captured == [
        {
            "reason": "ask",
            "round": 1,
            "question": "proceed with the actual operation?",
            "answers": ["approve", "reject"],
        }
    ]
    assert records[0].next_step == MANAGER_NEXT_ASK


def test_kernel_executor_prompt_uses_only_route_selected_related_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _events, records = _patch_kernel_boundaries(monkeypatch)
    manager = _QueueAgent(
        "manager",
        [
            "Next: cli\nTask: establish first audited context",
            "Next: cli\nTask: establish second audited context",
            """
Current task state:
Related audit reports: round_001 is a state-only fake reference
Task contract:
Preserve the selected audit boundary; round_001 is contract prose
Dependency assessment: use only the explicitly related second report
Next: cli
Task: consume selected related context
Related audit reports: round_002 because it is the actual dependency
Boundaries: do not inject unselected reports
""",
        ],
    )
    executor = _QueueAgent("executor", ["one", "two", "three"])
    auditor = _QueueAgent(
        "auditor",
        [
            _audit("incomplete") + "\nAUDIT_MARKER_ONE",
            _audit("incomplete") + "\nAUDIT_MARKER_TWO",
            _audit("incomplete") + "\nAUDIT_MARKER_THREE",
        ],
    )

    asyncio.run(
        manager_runtime._run_impl(
            task="related report authority",
            env=object(),
            config=_routing_config(tmp_path, rounds=3),
            manager_agent=manager,
            gui_executor_agents={"cheap": executor, "strong": executor},
            cli_executor_agents={"cheap": executor, "strong": executor},
            gui_auditor_agent=auditor,
            cli_auditor_agent=auditor,
        )
    )

    assert records[2].related_report_refs == ["round_002"]
    assert "AUDIT_MARKER_TWO" in executor.prompts[2]
    assert "AUDIT_MARKER_ONE" not in executor.prompts[2]


def test_kernel_routes_cheap_cheap_strong_and_persists_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events, records = _patch_kernel_boundaries(monkeypatch)
    calls: list[str] = []
    plans = ["Next: cli\nTask: same subtask"] * 3
    manager = _QueueAgent("manager", plans)
    auditor = _QueueAgent("auditor", [_audit("incomplete"), _audit("blocked"), _audit("complete")])
    cheap = _QueueAgent("cheap", ["cheap done", "cheap done"], calls)
    strong = _QueueAgent("strong", ["strong done"], calls)

    report = asyncio.run(
        manager_runtime._run_impl(
            task="route by cost",
            env=object(),
            config=_routing_config(tmp_path, rounds=3),
            manager_agent=manager,
            gui_executor_agents={"cheap": cheap, "strong": strong},
            cli_executor_agents={"cheap": cheap, "strong": strong},
            gui_auditor_agent=auditor,
            cli_auditor_agent=auditor,
        )
    )

    assert calls == ["cheap", "cheap", "strong"]
    assert [item.executor_routing["selected_tier"] for item in records] == [
        "cheap",
        "cheap",
        "strong",
    ]
    assert [item.executor_routing["audit_signal"] for item in records] == [
        "incomplete",
        "blocked",
        "complete",
    ]
    assert records[1].executor_routing["failures_after"] == 2
    assert records[2].executor_routing["selection_reason"] == "automatic_escalation"
    assert report["rounds"][2]["executor_routing"]["selected_tier"] == "strong"
    start = next(payload for event, payload in events if event == "role_harness_start")
    assert start["executor_routing"]["available_tiers"] == ["cheap", "strong"]
    audit_done = [payload for event, payload in events if event == "auditor_role_done"]
    assert audit_done[-1]["executor_routing"]["audit_signal"] == "complete"


def test_kernel_honors_manager_strong_new_task_reset_and_gui_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _events, records = _patch_kernel_boundaries(monkeypatch)
    calls: list[str] = []
    manager = _QueueAgent(
        "manager",
        [
            "Next: cli\nExecutor tier: strong\nTask: first task",
            "Next: cli\nTask: different task",
            "Next: gui\nTask: visual task",
        ],
    )
    auditor = _QueueAgent("auditor", [_audit("incomplete")] * 3)
    cli_cheap = _QueueAgent("cli-cheap", ["done"], calls)
    cli_strong = _QueueAgent("cli-strong", ["done"], calls)
    gui_cheap = _QueueAgent("gui-cheap", ["done"], calls)
    gui_strong = _QueueAgent("gui-strong", [], calls)

    asyncio.run(
        manager_runtime._run_impl(
            task="mixed routes",
            env=object(),
            config=_routing_config(tmp_path, rounds=3),
            manager_agent=manager,
            gui_executor_agents={"cheap": gui_cheap, "strong": gui_strong},
            cli_executor_agents={"cheap": cli_cheap, "strong": cli_strong},
            gui_auditor_agent=auditor,
            cli_auditor_agent=auditor,
        )
    )

    assert calls == ["cli-strong", "cli-cheap", "gui-cheap"]
    assert records[0].executor_routing["selection_reason"] == "manager_escalation"
    assert records[1].executor_routing["failures_before"] == 0
    assert records[2].executor_routing["failures_before"] == 0


def test_kernel_rejects_unknown_tier_without_executor_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events, records = _patch_kernel_boundaries(monkeypatch)
    calls: list[str] = []
    manager = _QueueAgent(
        "manager", ["Next: cli\nExecutor tier: unknown\nTask: rejected task"]
    )
    executor = _QueueAgent("executor", [], calls)
    auditor = _QueueAgent("auditor", [])

    report = asyncio.run(
        manager_runtime._run_impl(
            task="reject unknown",
            env=object(),
            config=_routing_config(tmp_path, rounds=1),
            manager_agent=manager,
            gui_executor_agents={"cheap": executor, "strong": executor},
            cli_executor_agents={"cheap": executor, "strong": executor},
            gui_auditor_agent=auditor,
            cli_auditor_agent=auditor,
        )
    )

    assert calls == []
    assert records[0].next_step == "invalid"
    assert records[0].executor_routing["selection_reason"] == "invalid"
    assert "Allowed executor tiers: cheap, strong" in records[0].harness_feedback
    assert report["rounds"][0]["executor_routing"]["requested_tier"] == "unknown"
    assert any(event == "executor_routing_rejected" for event, _ in events)


def test_kernel_auditor_timeout_does_not_increment_or_clear_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _events, records = _patch_kernel_boundaries(monkeypatch)
    manager = _QueueAgent("manager", ["Next: cli\nTask: same"] * 3)
    executor = _QueueAgent("executor", ["done"] * 3)
    auditor = _QueueAgent(
        "auditor",
        [
            _audit("incomplete"),
            EpisodeResult(status="timeout", error="audit timeout"),
            _audit("incomplete"),
        ],
    )

    asyncio.run(
        manager_runtime._run_impl(
            task="timeout accounting",
            env=object(),
            config=_routing_config(tmp_path, rounds=3),
            manager_agent=manager,
            gui_executor_agents={"cheap": executor, "strong": executor},
            cli_executor_agents={"cheap": executor, "strong": executor},
            gui_auditor_agent=auditor,
            cli_auditor_agent=auditor,
        )
    )

    assert records[0].executor_routing["failures_after"] == 1
    assert records[1].executor_routing["audit_signal"] == "not_counted"
    assert records[1].executor_routing["failures_after"] == 1
    assert records[2].executor_routing["failures_before"] == 1
    assert records[2].executor_routing["failures_after"] == 2


def test_kernel_rejected_format_repair_does_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _events, records = _patch_kernel_boundaries(monkeypatch)
    manager = _QueueAgent("manager", ["Next: cli\nTask: same"])
    executor = _QueueAgent("executor", ["done"])
    auditor = _QueueAgent("auditor", ["missing required control header"])
    repair = _QueueAgent("repair", ["still invalid"])

    asyncio.run(
        manager_runtime._run_impl(
            task="repair accounting",
            env=object(),
            config=_routing_config(tmp_path, rounds=1),
            manager_agent=manager,
            gui_executor_agents={"cheap": executor, "strong": executor},
            cli_executor_agents={"cheap": executor, "strong": executor},
            gui_auditor_agent=auditor,
            cli_auditor_agent=auditor,
            auditor_format_repair_agent=repair,
        )
    )

    assert records[0].auditor_status["format_repair_accepted"] is False
    assert records[0].executor_routing["audit_signal"] == "not_counted"
    assert records[0].executor_routing["failures_after"] == 0


def test_kernel_accepted_format_repair_counts_its_parsed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _events, records = _patch_kernel_boundaries(monkeypatch)
    manager = _QueueAgent("manager", ["Next: cli\nTask: same"])
    executor = _QueueAgent("executor", ["done"])
    auditor = _QueueAgent("auditor", ["missing required control header"])
    repair = _QueueAgent("repair", [_audit("incomplete")])

    asyncio.run(
        manager_runtime._run_impl(
            task="repair accounting",
            env=object(),
            config=_routing_config(tmp_path, rounds=1),
            manager_agent=manager,
            gui_executor_agents={"cheap": executor, "strong": executor},
            cli_executor_agents={"cheap": executor, "strong": executor},
            gui_auditor_agent=auditor,
            cli_auditor_agent=auditor,
            auditor_format_repair_agent=repair,
        )
    )

    assert records[0].auditor_status["format_repair_accepted"] is True
    assert records[0].executor_routing["audit_signal"] == "incomplete"
    assert records[0].executor_routing["failures_after"] == 1


@pytest.mark.parametrize(
    "case",
    ("executor_cancel", "executor_error", "auditor_cancel", "auditor_error", "repair_cancel"),
)
def test_kernel_early_failures_persist_not_counted_routing_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    _events, records = _patch_kernel_boundaries(monkeypatch)
    manager = _QueueAgent("manager", ["Next: cli\nTask: same"])
    executor_result: str | EpisodeResult = "done"
    auditor_result: str | EpisodeResult = _audit("incomplete")
    repair: _QueueAgent | None = None
    if case == "executor_cancel":
        executor_result = EpisodeResult(status="cancelled", error="cancelled")
    elif case == "executor_error":
        executor_result = EpisodeResult(status="error", error="executor failed")
    elif case == "auditor_cancel":
        auditor_result = EpisodeResult(status="cancelled", error="cancelled")
    elif case == "auditor_error":
        auditor_result = EpisodeResult(status="error", error="auditor failed")
    else:
        auditor_result = "missing required control header"
        repair = _QueueAgent(
            "repair", [EpisodeResult(status="cancelled", error="cancelled")]
        )
    executor = _QueueAgent("executor", [executor_result])
    auditor = _QueueAgent("auditor", [auditor_result])

    asyncio.run(
        manager_runtime._run_impl(
            task="early exit metadata",
            env=object(),
            config=_routing_config(tmp_path, rounds=1),
            manager_agent=manager,
            gui_executor_agents={"cheap": executor, "strong": executor},
            cli_executor_agents={"cheap": executor, "strong": executor},
            gui_auditor_agent=auditor,
            cli_auditor_agent=auditor,
            auditor_format_repair_agent=repair,
        )
    )

    assert len(records) == 1
    assert records[0].executor_routing["audit_signal"] == "not_counted"
    assert records[0].executor_routing["failures_after"] == 0


def test_kernel_routing_disabled_uses_legacy_singular_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _events, records = _patch_kernel_boundaries(monkeypatch)
    calls: list[str] = []
    manager = _QueueAgent("manager", ["Next: cli\nTask: legacy task"])
    executor = _QueueAgent("legacy", ["done"], calls)
    auditor = _QueueAgent("auditor", [_audit("incomplete")])
    config = HarnessConfig(
        max_total_episodes=1,
        workspace_path=str(tmp_path / "workspace"),
        harness_dir=str(tmp_path / "harness"),
        log_dir=str(tmp_path / "logs"),
    )

    asyncio.run(
        manager_runtime._run_impl(
            task="legacy",
            env=object(),
            config=config,
            manager_agent=manager,
            gui_executor_agent=executor,
            cli_executor_agent=executor,
            gui_auditor_agent=auditor,
            cli_auditor_agent=auditor,
        )
    )

    assert calls == ["legacy"]
    assert records[0].executor_routing["enabled"] is False


def test_kernel_tier_map_mismatch_fails_before_any_episode(tmp_path: Path) -> None:
    calls: list[str] = []
    agent = _QueueAgent("agent", [], calls)
    with pytest.raises(ValueError, match="identical keys"):
        asyncio.run(
            manager_runtime._run_impl(
                task="fail before start",
                env=object(),
                config=_routing_config(tmp_path, rounds=1),
                manager_agent=agent,
                gui_executor_agents={"cheap": agent, "strong": agent},
                cli_executor_agents={"cheap": agent},
                gui_auditor_agent=agent,
                cli_auditor_agent=agent,
            )
        )
    assert calls == []


@pytest.mark.parametrize(
    "case",
    (
        "policy_without_maps",
        "maps_without_policy",
        "missing_policy_tier",
        "empty_key",
        "padded_key",
    ),
)
def test_kernel_invalid_routing_setup_fails_before_any_episode(
    tmp_path: Path, case: str
) -> None:
    calls: list[str] = []
    agent = _QueueAgent("agent", [], calls)
    config = (
        _routing_config(tmp_path, rounds=1)
        if case != "maps_without_policy"
        else HarnessConfig(log_dir=str(tmp_path / "logs"))
    )
    kwargs: dict[str, object] = {}
    if case == "maps_without_policy":
        kwargs = {
            "gui_executor_agents": {"cheap": agent, "strong": agent},
            "cli_executor_agents": {"cheap": agent, "strong": agent},
        }
    elif case == "missing_policy_tier":
        kwargs = {
            "gui_executor_agents": {"cheap": agent},
            "cli_executor_agents": {"cheap": agent},
        }
    elif case == "empty_key":
        kwargs = {
            "gui_executor_agents": {"": agent, "cheap": agent, "strong": agent},
            "cli_executor_agents": {"": agent, "cheap": agent, "strong": agent},
        }
    elif case == "padded_key":
        kwargs = {
            "gui_executor_agents": {
                "cheap": agent,
                "strong": agent,
                " premium ": agent,
            },
            "cli_executor_agents": {
                "cheap": agent,
                "strong": agent,
                " premium ": agent,
            },
        }

    with pytest.raises(ValueError):
        asyncio.run(
            manager_runtime._run_impl(
                task="fail before start",
                env=object(),
                config=config,
                manager_agent=agent,
                gui_auditor_agent=agent,
                cli_auditor_agent=agent,
                **kwargs,
            )
        )
    assert calls == []


def test_kernel_manager_runtime_failure_precedes_routing_decision_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _events, records = _patch_kernel_boundaries(monkeypatch)
    calls: list[str] = []
    manager = _QueueAgent(
        "manager", [EpisodeResult(status="error", error="manager failed")]
    )
    executor = _QueueAgent("executor", [], calls)
    auditor = _QueueAgent("auditor", [])

    asyncio.run(
        manager_runtime._run_impl(
            task="manager failure",
            env=object(),
            config=_routing_config(tmp_path, rounds=1),
            manager_agent=manager,
            gui_executor_agents={"cheap": executor, "strong": executor},
            cli_executor_agents={"cheap": executor, "strong": executor},
            gui_auditor_agent=auditor,
            cli_auditor_agent=auditor,
        )
    )

    assert calls == []
    assert len(records) == 1
    assert records[0].manager_status["status"] == "error"
    assert records[0].executor_routing == {}


def test_cli_main_carries_project_executor_routing_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = ExecutorRoutingConfig("cheap", 2, "strong")
    tiers = {
        "cheap": {"model": "cheap-model"},
        "strong": {"agent": "claude_code"},
    }
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "load_run_defaults",
        lambda: {
            "executor_tiers": tiers,
            "executor_routing": policy,
            "dashboard": False,
        },
    )
    monkeypatch.setattr(cli, "_run_command", lambda args: captured.update(vars(args)) or 19)

    assert cli.main(["run", "--task", "inspect routing defaults"]) == 19
    assert captured["executor_tiers"] is tiers
    assert captured["executor_routing"] is policy


@pytest.mark.parametrize(
    ("route_role", "tier_spec", "overrides", "expected"),
    (
        (
            "gui_executor",
            {"agent": "claude_code"},
            {"gui_executor_agent": "codex", "gui_executor_model": "codex-model"},
            ("claude_code", None),
        ),
        (
            "gui_executor",
            {"agent": "codex"},
            {"gui_executor_agent": "codex", "gui_executor_model": "codex-model"},
            ("codex", "codex-model"),
        ),
        (
            "gui_executor",
            {"model": "tier-model"},
            {"gui_executor_agent": "opencode"},
            ("opencode", "tier-model"),
        ),
        (
            "cli_executor",
            {},
            {"executor_agent": "claude_code", "executor_model": "executor-model"},
            ("claude_code", "executor-model"),
        ),
        (
            "gui_executor",
            {},
            {"gui_executor_agent": "opencode", "gui_executor_model": "gui-model"},
            ("opencode", "gui-model"),
        ),
        (
            "cli_executor",
            {},
            {"cli_executor_agent": "deepseek_harness", "cli_executor_model": "cli-model"},
            ("deepseek_harness", "cli-model"),
        ),
    ),
)
def test_cli_executor_tier_binding_fallbacks(
    route_role: str,
    tier_spec: dict[str, str],
    overrides: dict[str, object],
    expected: tuple[str, str | None],
) -> None:
    args = _cli_binding_args(**overrides)
    assert cli._resolve_executor_tier_binding(args, route_role, tier_spec) == expected


def test_cli_executor_tier_backend_boundary_does_not_inherit_global_model() -> None:
    args = _cli_binding_args(executor_agent="claude_code")
    assert cli._resolve_role_model(args, "gui_executor") is None
    assert cli._resolve_executor_tier_binding(
        args, "gui_executor", {"agent": "opencode"}
    ) == ("opencode", None)


def test_cli_enabled_builds_permission_separated_tier_maps_without_singulars() -> None:
    policy = ExecutorRoutingConfig("cheap", 2, "strong")
    args = _cli_binding_args(
        executor_routing=policy,
        executor_tiers={
            "cheap": {"model": "cheap-model"},
            "strong": {"agent": "claude_code"},
        },
        gui_executor_agent="codex",
        cli_executor_agent="opencode",
    )
    calls: list[tuple[str, str, str | None]] = []

    def build(permission: str, backend: str, model: str | None) -> object:
        binding = (permission, backend, model)
        calls.append(binding)
        return binding

    built = cli._build_executor_agent_kwargs(args, build)

    assert set(built) == {"gui_executor_agents", "cli_executor_agents"}
    assert built["gui_executor_agents"] == {
        "cheap": ("gui_executor", "codex", "cheap-model"),
        "strong": ("gui_executor", "claude_code", None),
    }
    assert built["cli_executor_agents"] == {
        "cheap": ("cli_executor", "opencode", "cheap-model"),
        "strong": ("cli_executor", "claude_code", None),
    }
    assert len(calls) == 4


@pytest.mark.parametrize("tiers", (None, {}))
def test_cli_enabled_requires_tiers_before_building_agents(tiers: object) -> None:
    args = _cli_binding_args(
        executor_routing=ExecutorRoutingConfig("cheap", 2, "strong"),
        executor_tiers=tiers,
    )
    calls: list[tuple[str, str, str | None]] = []

    with pytest.raises(ValueError, match="requires configured executor tiers"):
        cli._build_executor_agent_kwargs(
            args,
            lambda permission, backend, model: calls.append(
                (permission, backend, model)
            ),
        )
    assert calls == []


@pytest.mark.parametrize("with_empty_config_attrs", (False, True))
def test_cli_disabled_builds_legacy_singular_executors(
    with_empty_config_attrs: bool,
) -> None:
    overrides: dict[str, object] = {
        "gui_executor_agent": "codex",
        "gui_executor_model": "gui-model",
        "cli_executor_agent": "claude_code",
        "cli_executor_model": "cli-model",
    }
    if with_empty_config_attrs:
        overrides.update(executor_routing=None, executor_tiers=None)
    args = _cli_binding_args(**overrides)
    calls: list[tuple[str, str, str | None]] = []

    def build(permission: str, backend: str, model: str | None) -> object:
        binding = (permission, backend, model)
        calls.append(binding)
        return binding

    built = cli._build_executor_agent_kwargs(args, build)

    assert built == {
        "gui_executor_agent": ("gui_executor", "codex", "gui-model"),
        "cli_executor_agent": ("cli_executor", "claude_code", "cli-model"),
    }
    assert len(calls) == 2
