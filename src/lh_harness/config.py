from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

from .types import ExecutorRoutingConfig, MAX_ROUNDS

try:
    tomllib = importlib.import_module("tomllib")
except ModuleNotFoundError:
    tomllib = importlib.import_module("tomli")

PROJECT_CONFIG_PATH = Path(".lh-harness/config.toml")

_AGENT_CHOICES = {"claude_code", "codex", "deepseek_harness", "opencode"}
_ROLE_NAMES = {
    "manager",
    "executor",
    "gui_executor",
    "cli_executor",
    "auditor",
    "gui_auditor",
    "cli_auditor",
    "final_response",
}
_TIMEOUT_NAMES = {"manager", "gui_executor", "cli_executor", "auditor"}
_RUN_KEYS = {
    "agent",
    "model",
    "reasoning_effort",
    "env",
    "runs_root",
    "workspace",
    "harness_dir",
    "log_dir",
    "base_url",
    "prompt_language",
    "claude_mcp_config",
    "codex_mcp_config",
    "mcp_add_dirs",
    "guard_exclude_paths",
    "max_rounds",
    "dashboard",
    "dashboard_port",
    "roles",
    "timeouts",
    "executor_routing",
}
_STRING_KEYS = {
    "model",
    "runs_root",
    "workspace",
    "harness_dir",
    "log_dir",
    "base_url",
    "claude_mcp_config",
    "codex_mcp_config",
}
_EXECUTOR_ROUTING_KEYS = {
    "default_tier",
    "escalate_after_failures",
    "escalation_tier",
}
_EXECUTOR_TIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

CONFIG_TEMPLATE = """# LongHorizon-Harness project defaults.
# Explicit CLI arguments override these values.

[run]
agent = "codex"
model = "gpt-5.6-sol"

# Reasoning depth, forwarded to whichever backend exposes it (Codex through
# `model_reasoning_effort`, Claude Code through `--effort`, OpenCode through
# `--variant`). Any value the backend accepts is allowed, so a newer tier does
# not need a harness release. Leave unset to keep the provider's own setting.
# reasoning_effort = "high"

env = "local"
runs_root = "./.lh-harness/runs"
# Agents work in the directory lh-harness was started from unless set here.
# workspace = "./workspace"
# harness_dir = "./.lh-harness/runs/<run-id>/harness"
# log_dir = "./lh_harness"

# base_url = "https://api.example.com/v1"

prompt_language = "en"
# Each agent reads its own format; installed plugins are loaded automatically.
# claude_mcp_config = "/path/to/mcp.json"
# codex_mcp_config = "/path/to/mcp.toml"
mcp_add_dirs = []

# Build/cache directories the auditor read-only guard should not snapshot,
# e.g. ["target", "node_modules", "build", ".venv"]. Agents can still read
# them. Exclusions must stay inside the workspace; ".git" and harness-owned
# control/state paths are rejected at startup, and the effective list is
# echoed at run start and recorded in each audited episode's metadata.
# Passing --guard-exclude-path replaces this list rather than adding to it.
guard_exclude_paths = []

max_rounds = 25
dashboard = true
# Embedded dashboards use an OS-assigned port by default so concurrent runs
# cannot accidentally share or race a fixed listener. Standalone `web` keeps
# its explicit 8799 default for the operator-facing control plane.
dashboard_port = 0

[run.timeouts]
manager = 300
gui_executor = 1800
cli_executor = 1800
auditor = 300

[run.roles.manager]
# agent = "codex"
# model = "gpt-5.6-sol"
# reasoning_effort = "high"

[run.roles.executor]
# agent = "codex"
# model = "gpt-5.6-sol"

# Optional cost-aware executor routing. Tier-specific fields override the
# parent executor role; omitted fields inherit from [run.roles.executor].
# [run.roles.executor.cheap]
# agent = "deepseek_harness"
# model = "deepseek-v4-flash"
#
# [run.roles.executor.strong]
# agent = "codex"
# model = "gpt-5.6-sol"
#
# [run.executor_routing]
# default_tier = "cheap"
# escalate_after_failures = 2
# escalation_tier = "strong"

[run.roles.gui_executor]
# agent = "codex"
# model = "gpt-5.6-sol"

[run.roles.cli_executor]
# agent = "codex"
# model = "gpt-5.6-sol"

[run.roles.auditor]
# agent = "codex"
# model = "gpt-5.6-sol"

[run.roles.gui_auditor]
# agent = "codex"
# model = "gpt-5.6-sol"

[run.roles.cli_auditor]
# agent = "codex"
# model = "gpt-5.6-sol"

# Writes the closing reply to you; falls back to the manager's agent/model.
[run.roles.final_response]
# agent = "codex"
# model = "gpt-5.6-sol"
"""


class ProjectConfigError(ValueError):
    pass


def create_project_config(
    path: str | Path = PROJECT_CONFIG_PATH,
    *,
    force: bool = False,
) -> Path:
    target = Path(path)
    if target.exists() and not force:
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return target


def load_run_defaults(path: str | Path = PROJECT_CONFIG_PATH) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        return {}
    try:
        with source.open("rb") as fh:
            payload = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProjectConfigError(f"could not read {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProjectConfigError(f"{source} must contain a TOML table")
    unknown_root = set(payload) - {"run"}
    if unknown_root:
        raise ProjectConfigError(f"unknown top-level key(s): {_names(unknown_root)}")
    run = payload.get("run", {})
    if not isinstance(run, dict):
        raise ProjectConfigError("[run] must be a TOML table")
    return _flatten_run_table(run)


def _flatten_run_table(run: dict[str, Any]) -> dict[str, Any]:
    unknown = set(run) - _RUN_KEYS
    if unknown:
        raise ProjectConfigError(f"unknown [run] key(s): {_names(unknown)}")

    defaults: dict[str, Any] = {}
    for key in _STRING_KEYS:
        if key in run:
            defaults[key] = _string(run[key], f"run.{key}")

    if "agent" in run:
        defaults["agent"] = _choice(run["agent"], "run.agent", _AGENT_CHOICES)
    if "reasoning_effort" in run:
        defaults["reasoning_effort"] = _reasoning_effort(
            run["reasoning_effort"], "run.reasoning_effort"
        )
    if "env" in run:
        defaults["env"] = _choice(run["env"], "run.env", {"local"})
    if "prompt_language" in run:
        defaults["prompt_language"] = _choice(
            run["prompt_language"], "run.prompt_language", {"en", "zh"}
        )
    if "max_rounds" in run:
        defaults["max_rounds"] = _positive_int(run["max_rounds"], "run.max_rounds")
    if "dashboard" in run:
        defaults["dashboard"] = _boolean(run["dashboard"], "run.dashboard")
    if "dashboard_port" in run:
        defaults["dashboard_port"] = _port(run["dashboard_port"], "run.dashboard_port")
    if "mcp_add_dirs" in run:
        value = run["mcp_add_dirs"]
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ProjectConfigError("run.mcp_add_dirs must be an array of non-empty strings")
        defaults["mcp_add_dir"] = list(value)
    if "guard_exclude_paths" in run:
        value = run["guard_exclude_paths"]
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ProjectConfigError("run.guard_exclude_paths must be an array of non-empty strings")
        defaults["guard_exclude_path"] = list(value)

    roles = run.get("roles", {})
    if not isinstance(roles, dict):
        raise ProjectConfigError("[run.roles] must be a TOML table")
    unknown_roles = set(roles) - _ROLE_NAMES
    if unknown_roles:
        raise ProjectConfigError(f"unknown role(s): {_names(unknown_roles)}")
    for role, values in roles.items():
        if not isinstance(values, dict):
            raise ProjectConfigError(f"[run.roles.{role}] must be a TOML table")
        role_keys = {"agent", "model", "reasoning_effort"}
        tier_names = set(values) - role_keys if role == "executor" else set()
        unknown_role_keys = set(values) - role_keys - tier_names
        if unknown_role_keys:
            raise ProjectConfigError(
                f"unknown [run.roles.{role}] key(s): {_names(unknown_role_keys)}"
            )
        if "agent" in values:
            defaults[f"{role}_agent"] = _choice(
                values["agent"], f"run.roles.{role}.agent", _AGENT_CHOICES
            )
        if "model" in values:
            defaults[f"{role}_model"] = _string(
                values["model"], f"run.roles.{role}.model"
            )
        if "reasoning_effort" in values:
            defaults[f"{role}_reasoning_effort"] = _reasoning_effort(
                values["reasoning_effort"], f"run.roles.{role}.reasoning_effort"
            )
        if role == "executor" and tier_names:
            defaults["executor_tiers"] = _executor_tiers(values, tier_names)

    routing_value = run.get("executor_routing")
    routing = _executor_routing(routing_value) if routing_value is not None else None
    tiers = defaults.get("executor_tiers")
    if tiers is not None and routing is None:
        raise ProjectConfigError(
            "run.executor_routing is required when run.roles.executor tiers are configured"
        )
    if routing is not None:
        if tiers is None:
            raise ProjectConfigError(
                "run.roles.executor must configure at least two tiers when run.executor_routing is configured"
            )
        if len(tiers) < 2:
            raise ProjectConfigError(
                "run.roles.executor must configure at least two tiers when run.executor_routing is configured"
            )
        if routing.default_tier not in tiers:
            raise ProjectConfigError(
                "run.executor_routing.default_tier must reference a configured run.roles.executor tier"
            )
        if routing.escalation_tier not in tiers:
            raise ProjectConfigError(
                "run.executor_routing.escalation_tier must reference a configured run.roles.executor tier"
            )
        defaults["executor_routing"] = routing

    timeouts = run.get("timeouts", {})
    if not isinstance(timeouts, dict):
        raise ProjectConfigError("[run.timeouts] must be a TOML table")
    unknown_timeouts = set(timeouts) - _TIMEOUT_NAMES
    if unknown_timeouts:
        raise ProjectConfigError(f"unknown timeout role(s): {_names(unknown_timeouts)}")
    for role, value in timeouts.items():
        defaults[f"{role}_timeout"] = _positive_int(value, f"run.timeouts.{role}")
    return defaults


def _executor_tiers(
    executor_role: dict[str, Any], tier_names: set[str]
) -> dict[str, dict[str, str]]:
    tiers: dict[str, dict[str, str]] = {}
    for tier_name in sorted(tier_names):
        tier_path = f"run.roles.executor.{tier_name}"
        if not _EXECUTOR_TIER_PATTERN.fullmatch(tier_name):
            raise ProjectConfigError(
                f"{tier_path} must use a 1-64 character tier name containing only letters, digits, '_' or '-', starting with a letter or digit"
            )
        value = executor_role[tier_name]
        if not isinstance(value, dict):
            raise ProjectConfigError(f"[{tier_path}] must be a TOML table")
        unknown = set(value) - {"agent", "model"}
        if unknown:
            raise ProjectConfigError(
                f"unknown [{tier_path}] key(s): {_names(unknown)}"
            )
        parsed: dict[str, str] = {}
        if "agent" in value:
            parsed["agent"] = _choice(
                value["agent"], f"{tier_path}.agent", _AGENT_CHOICES
            )
        if "model" in value:
            parsed["model"] = _string(value["model"], f"{tier_path}.model")
        tiers[tier_name] = parsed
    return tiers


def _executor_routing(value: Any) -> ExecutorRoutingConfig:
    path = "run.executor_routing"
    if not isinstance(value, dict):
        raise ProjectConfigError(f"[{path}] must be a TOML table")
    unknown = set(value) - _EXECUTOR_ROUTING_KEYS
    if unknown:
        raise ProjectConfigError(f"unknown [{path}] key(s): {_names(unknown)}")
    missing = _EXECUTOR_ROUTING_KEYS - set(value)
    if missing:
        raise ProjectConfigError(f"missing [{path}] key(s): {_names(missing)}")
    default_tier = _tier_reference(value["default_tier"], f"{path}.default_tier")
    escalation_tier = _tier_reference(
        value["escalation_tier"], f"{path}.escalation_tier"
    )
    failures = _positive_int(
        value["escalate_after_failures"], f"{path}.escalate_after_failures"
    )
    if default_tier == escalation_tier:
        raise ProjectConfigError(
            "run.executor_routing.default_tier and run.executor_routing.escalation_tier must be different"
        )
    return ExecutorRoutingConfig(
        default_tier=default_tier,
        escalate_after_failures=failures,
        escalation_tier=escalation_tier,
    )


def _tier_reference(value: Any, name: str) -> str:
    result = _string(value, name)
    if not _EXECUTOR_TIER_PATTERN.fullmatch(result):
        raise ProjectConfigError(
            f"{name} must name a configured executor tier using letters, digits, '_' or '-'"
        )
    return result


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectConfigError(f"{name} must be a non-empty string")
    return value


def _choice(value: Any, name: str, choices: set[str]) -> str:
    result = _string(value, name)
    if result not in choices:
        raise ProjectConfigError(f"{name} must be one of: {_names(choices)}")
    return result


def _reasoning_effort(value: Any, name: str) -> str:
    from .agent_registry import normalise_reasoning_effort

    try:
        return normalise_reasoning_effort(_string(value, name))
    except ValueError as exc:
        raise ProjectConfigError(f"{name}: {exc}") from exc


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProjectConfigError(f"{name} must be an integer of at least 1")
    if name.endswith("max_rounds") and value > MAX_ROUNDS:
        raise ProjectConfigError(f"{name} must be at most {MAX_ROUNDS}")
    return value


def _port(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
        raise ProjectConfigError(f"{name} must be an integer from 0 to 65535")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ProjectConfigError(f"{name} must be true or false")
    return value


def _names(values: set[str]) -> str:
    return ", ".join(sorted(values))
