from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


def _launch_directory() -> str:
    """Directory lh-harness was started from, captured once at import.

    Symlinks stay resolved so this agrees with the run paths derived from it,
    which the agent sandbox rules are matched against.
    """
    try:
        return str(Path.cwd())
    except OSError:
        # The directory was deleted. Importing must still succeed so the CLI can
        # report that clearly instead of failing with an opaque ImportError.
        return os.environ.get("PWD") or "."


# Harness bookkeeping is user-scoped; the agents work in the directory
# lh-harness was started from, so a task acts on the caller's real project.
DEFAULT_STATE_ROOT = str(Path.home() / ".lh-harness")
DEFAULT_WORKSPACE_PATH = _launch_directory()
DEFAULT_HARNESS_DIR = f"{DEFAULT_STATE_ROOT}/harness"
DEFAULT_LOG_DIR = f"{DEFAULT_STATE_ROOT}/lh_harness"
DEFAULT_TMP_DIR = f"{DEFAULT_STATE_ROOT}/tmp"

DEFAULT_CLAUDE_MODEL = "claude-opus-5"
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_DEEPSEEK_HARNESS_MODEL = "deepseek-v4-flash"
DEFAULT_OPENCODE_MODEL = "opencode/deepseek-v4-flash-free"

# Normalized effort scale shared by every backend, covering the union of the
# levels the supported CLIs document.  "Effort" follows Anthropic's umbrella
# term (distinct from the separate thinking control); on backends whose only
# dial is reasoning-specific it drives that dial.  Each adapter translates a
# level into its own vocabulary (Codex `model_reasoning_effort`, Claude Code
# CLAUDE_CODE_EFFORT_LEVEL, OpenCode `--variant`, DeepSeek Harness
# `reasoningEffort`), mapping a level its backend lacks to the nearest
# supported one; the episode metadata records both the requested and the
# effective value so the substitution stays visible.
EFFORT_CHOICES: tuple[str, ...] = ("min", "low", "med", "high", "xhigh", "max")

# A run is intentionally bounded at every ingress point.  Without a shared
# ceiling, a malformed Web/CLI request can reserve an effectively unbounded
# amount of work and disk/event pressure.  Keep this in the protocol-adjacent
# types module so CLI, config, supervisor, and Web validation use one value.
MAX_ROUNDS = 1000
DEFAULT_MAX_ROUNDS = 25

RoleNextStep = Literal["gui", "cli", "done", "blocked", "invalid", "ask"]
PromptLanguage = Literal["en", "zh"]


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    termination_reason: str | None = None


@dataclass
class EpisodeBudget:
    max_duration_seconds: int = 1800

    def __post_init__(self) -> None:
        if self.max_duration_seconds < 1:
            raise ValueError("max_duration_seconds must be at least 1")


@dataclass
class EpisodeResult:
    status: Literal["done", "timeout", "error", "cancelled"]
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
    contract_audit_status: Literal["aligned", "unknown", "needs_revision", "invalid"] = "unknown"
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
    task_contract: str = ""
    related_report_refs: list[str] = field(default_factory=list)
    manager_status: dict[str, Any] = field(default_factory=dict)
    executor_status: dict[str, Any] = field(default_factory=dict)
    auditor_status: dict[str, Any] = field(default_factory=dict)


@dataclass
class HarnessConfig:
    max_total_episodes: int = 4
    manager_budget: EpisodeBudget = field(
        default_factory=lambda: EpisodeBudget(max_duration_seconds=300)
    )
    gui_executor_budget: EpisodeBudget = field(default_factory=EpisodeBudget)
    cli_executor_budget: EpisodeBudget = field(default_factory=EpisodeBudget)
    auditor_budget: EpisodeBudget = field(
        default_factory=lambda: EpisodeBudget(max_duration_seconds=300)
    )
    workspace_path: str = DEFAULT_WORKSPACE_PATH
    harness_dir: str = DEFAULT_HARNESS_DIR
    log_dir: str = DEFAULT_LOG_DIR
    auditor_output_chars: int = 24_000
    role_verified_context_chars: int = 60_000
    role_history_chars: int = 100_000
    # English is the production default; Chinese remains available for
    # OSWorldv2-compatible role prompts and operator-facing control headers.
    prompt_language: PromptLanguage = "en"


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
        contract_audit_status=(
            data.get("contract_audit_status")
            if data.get("contract_audit_status") in {"aligned", "unknown", "needs_revision", "invalid"}
            else "unknown"
        ),
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
