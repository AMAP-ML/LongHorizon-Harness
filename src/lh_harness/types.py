from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

RoleNextStep = Literal["gui", "cli", "done", "blocked", "invalid", "ask"]


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int


@dataclass
class EpisodeBudget:
    max_turns: int = 20
    max_duration_seconds: int = 1800


@dataclass
class EpisodeResult:
    status: Literal["done", "timeout", "error"]
    actions_log: str = ""
    error: str | None = None
    duration_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditReport:
    round_id: str
    status: Literal["complete", "incomplete", "blocked"]
    report_text: str = ""
    state_summary: str = ""
    completed: list[dict[str, str]] = field(default_factory=list)
    missing: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[dict[str, str]] = field(default_factory=list)
    action_guidance: str = ""
    raw_commands: list[dict[str, str]] = field(default_factory=list)
    integrity_status: Literal["clean", "suspect", "violation"] = "clean"
    integrity_findings: list[dict[str, Any]] = field(default_factory=list)
    artifact_actions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ManagedRound:
    round_index: int
    next_step: RoleNextStep
    plan_text: str
    executor_output: str = ""
    auditor_report: str = ""
    harness_feedback: str = ""
    task_state: str = ""
    related_report_refs: list[str] = field(default_factory=list)
    executor_status: dict[str, Any] = field(default_factory=dict)
    auditor_status: dict[str, Any] = field(default_factory=dict)


@dataclass
class HarnessConfig:
    max_total_episodes: int = 4
    episode_budget: EpisodeBudget = field(default_factory=EpisodeBudget)
    auditor_budget: EpisodeBudget = field(
        default_factory=lambda: EpisodeBudget(max_turns=20, max_duration_seconds=300)
    )
    workspace_path: str = "/tmp_workspace"
    harness_dir: str = "/tmp_workspace/.harness"
    log_dir: str = "./harness_logs"
    output_truncation_chars: int = 4096
    auditor_context_chars: int = 20_000
    auditor_output_chars: int = 24_000
    auditor_prompt_chars: int = 60_000
    auditor_model: str = "gpt-4o-mini"
    agent_model: str = "gpt-4o"
    api_key: str | None = None
    base_url: str | None = None
    manager_budget: EpisodeBudget | None = None
    gui_executor_budget: EpisodeBudget | None = None
    cli_executor_budget: EpisodeBudget | None = None
    role_auditor_budget: EpisodeBudget | None = None
    role_verified_context_chars: int = 60_000
    role_history_chars: int = 100_000
    # Kept for compatibility with WeaveBench wrappers that expose the newer
    # role-memory knob. V2 does not use a separate rolling memory prompt.
    role_memory_chars: int = 0

    def effective_ignored_path_prefixes(self) -> list[str]:
        workspace = self.workspace_path.rstrip("/")
        harness = self.harness_dir.rstrip("/")
        return [
            harness,
            f"{workspace}/.harness",
            f"{workspace}/gt",
            "/home/user/.claude",
            "/tmp/weavebench_claudecode_install",
            "/tmp/lh_harness_claudecode_auditor_bin",
            "/tmp/lh_harness_claudecode_auditor_py",
        ]

    def ground_truth_path_prefixes(self) -> list[str]:
        workspace = self.workspace_path.rstrip("/")
        return [f"{workspace}/gt", "/tmp_workspace/gt"]


def audit_report_to_dict(report: AuditReport) -> dict[str, Any]:
    return asdict(report)


def audit_report_from_dict(data: dict[str, Any]) -> AuditReport:
    status = data.get("status")
    if status not in {"complete", "incomplete", "blocked"}:
        status = "incomplete"
    integrity_status = data.get("integrity_status")
    if integrity_status not in {"clean", "suspect", "violation"}:
        integrity_status = "clean"
    return AuditReport(
        round_id=str(data.get("round_id") or ""),
        status=status,
        report_text=str(data.get("report_text") or data.get("report") or ""),
        state_summary=str(data.get("state_summary") or ""),
        completed=[
            {str(key): str(value) for key, value in item.items()}
            for item in data.get("completed", [])
            if isinstance(item, dict)
        ],
        missing=[
            {str(key): _coerce_missing_value(key, value) for key, value in item.items()}
            for item in data.get("missing", [])
            if isinstance(item, dict)
        ],
        blockers=[
            {str(key): str(value) for key, value in item.items()}
            for item in data.get("blockers", [])
            if isinstance(item, dict)
        ],
        action_guidance=str(data.get("action_guidance") or ""),
        raw_commands=[
            {str(key): str(value) for key, value in item.items()}
            for item in data.get("raw_commands", [])
            if isinstance(item, dict)
        ],
        integrity_status=integrity_status,
        integrity_findings=[
            {str(key): _coerce_record_value(value) for key, value in item.items()}
            for item in data.get("integrity_findings", [])
            if isinstance(item, dict)
        ],
        artifact_actions=[
            {str(key): _coerce_record_value(value) for key, value in item.items()}
            for item in data.get("artifact_actions", [])
            if isinstance(item, dict)
        ],
    )


def _coerce_missing_value(key: object, value: Any) -> Any:
    if key == "actionable":
        return bool(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _coerce_record_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _coerce_record_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_coerce_record_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
