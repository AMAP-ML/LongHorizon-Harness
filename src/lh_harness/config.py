from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from .types import EXECUTOR_TIERS, MAX_ROUNDS

try:
    tomllib = importlib.import_module("tomllib")
except ModuleNotFoundError:
    tomllib = importlib.import_module("tomli")

PROJECT_CONFIG_PATH = Path(".lh-harness/config.toml")

_AGENT_CHOICES = {"claude_code", "codex"}
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
# Executor roles may additionally carry per-tier sub-tables, so
# `[run.roles.executor.cheap]` flattens to the `executor_cheap` role.
_TIERED_ROLE_NAMES = {"executor", "gui_executor", "cli_executor"}
_TIMEOUT_NAMES = {"manager", "gui_executor", "cli_executor", "auditor"}
_EXECUTOR_ROUTING_KEYS = {
    "default_tier",
    "escalate_after_failures",
    "escalate_after_stalled_rounds",
    "escalation_tier",
    "escalation_briefing",
}
_RUN_KEYS = {
    "agent",
    "model",
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

CONFIG_TEMPLATE = """# LongHorizon-Harness project defaults.
# Explicit CLI arguments override these values.

[run]
agent = "codex"
model = "gpt-5.6-sol"

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

# Which executor tier runs a subtask. The manager may ask for one; otherwise
# default_tier applies. After escalate_after_failures consecutive failed audits
# the harness switches to escalation_tier itself, and drops back to the default
# once an audit passes. Set escalate_after_failures = 0 to turn that off.
[run.executor_routing]
default_tier = "cheap"
# Audits that report a real problem: blocked, suspect/violated integrity, or a
# contract needing revision. An audit that is merely `incomplete` with clean
# integrity is normal mid-run progress and is NOT counted here. 1 escalates on
# the first such failure; raising it retries on the cheap tier first, which costs
# less but is slower -- and a same-tier retry is NOT briefed on what just failed
# (only the escalated executor is), so it may well repeat it.
escalate_after_failures = 1
# The other shape of trouble: clean rounds that keep reporting the same
# outstanding gap without closing it. Escalates after this many in a row.
escalate_after_stalled_rounds = 3
escalation_tier = "strong"
# Tell the escalated executor what the previous tier already tried and why it
# was rejected. Each episode is a fresh session, so without this it starts blind.
# escalation_briefing = true

[run.roles.manager]
# agent = "codex"
# model = "gpt-5.6-sol"

[run.roles.executor]
# agent = "codex"
# model = "gpt-5.6-sol"

# Per-tier executors. Leave them out to run every tier on the same backend,
# which is what a configuration without tiers does today.
[run.roles.executor.cheap]
# agent = "codex"
# model = "gpt-5.6-sol"

[run.roles.executor.strong]
# agent = "claude_code"
# model = "claude-opus-5"

[run.roles.gui_executor]
# agent = "codex"
# model = "gpt-5.6-sol"

# Tier overrides for one executor type only; these win over [run.roles.executor.*].
# [run.roles.gui_executor.cheap]
# [run.roles.gui_executor.strong]

[run.roles.cli_executor]
# agent = "codex"
# model = "gpt-5.6-sol"

# [run.roles.cli_executor.cheap]
# [run.roles.cli_executor.strong]

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

    roles = run.get("roles", {})
    if not isinstance(roles, dict):
        raise ProjectConfigError("[run.roles] must be a TOML table")
    unknown_roles = set(roles) - _ROLE_NAMES
    if unknown_roles:
        # The CLI spells a tier `--executor-cheap-agent`, so the flat role name is
        # an easy guess. Point at the nested table rather than just rejecting it.
        flattened = sorted(
            name
            for name in unknown_roles
            if any(name == f"{role}_{tier}" for role in _TIERED_ROLE_NAMES for tier in EXECUTOR_TIERS)
        )
        if flattened:
            suggestions = ", ".join(
                f"[run.roles.{name.rsplit('_', 1)[0]}.{name.rsplit('_', 1)[1]}]" for name in flattened
            )
            raise ProjectConfigError(
                f"unknown role(s): {_names(unknown_roles)}; write executor tiers as a "
                f"nested table instead: {suggestions}"
            )
        raise ProjectConfigError(f"unknown role(s): {_names(unknown_roles)}")
    for role, values in roles.items():
        if not isinstance(values, dict):
            raise ProjectConfigError(f"[run.roles.{role}] must be a TOML table")
        # A tier sub-table is the only nested form; everything else at this
        # level is still just agent/model.
        tiers = {key: value for key, value in values.items() if isinstance(value, dict)}
        if tiers and role not in _TIERED_ROLE_NAMES:
            raise ProjectConfigError(
                f"[run.roles.{role}] does not take executor tiers; "
                f"tiers apply to: {_names(_TIERED_ROLE_NAMES)}"
            )
        unknown_tiers = set(tiers) - set(EXECUTOR_TIERS)
        if unknown_tiers:
            raise ProjectConfigError(
                f"unknown [run.roles.{role}] tier(s): {_names(unknown_tiers)}; "
                f"expected: {', '.join(EXECUTOR_TIERS)}"
            )
        for tier, tier_values in tiers.items():
            _flatten_role_table(defaults, f"{role}_{tier}", f"run.roles.{role}.{tier}", tier_values)
        _flatten_role_table(
            defaults,
            role,
            f"run.roles.{role}",
            {key: value for key, value in values.items() if key not in tiers},
        )

    routing = run.get("executor_routing", {})
    if not isinstance(routing, dict):
        raise ProjectConfigError("[run.executor_routing] must be a TOML table")
    unknown_routing = set(routing) - _EXECUTOR_ROUTING_KEYS
    if unknown_routing:
        raise ProjectConfigError(
            f"unknown [run.executor_routing] key(s): {_names(unknown_routing)}"
        )
    for key in ("default_tier", "escalation_tier"):
        if key in routing:
            defaults[f"executor_{key}"] = _choice(
                routing[key], f"run.executor_routing.{key}", set(EXECUTOR_TIERS)
            )
    for key in ("escalate_after_failures", "escalate_after_stalled_rounds"):
        if key in routing:
            # 0 is meaningful here: it turns that escalation signal off.
            defaults[f"executor_{key}"] = _non_negative_int(
                routing[key], f"run.executor_routing.{key}"
            )
    if "escalation_briefing" in routing:
        defaults["executor_escalation_briefing"] = _boolean(
            routing["escalation_briefing"], "run.executor_routing.escalation_briefing"
        )

    timeouts = run.get("timeouts", {})
    if not isinstance(timeouts, dict):
        raise ProjectConfigError("[run.timeouts] must be a TOML table")
    unknown_timeouts = set(timeouts) - _TIMEOUT_NAMES
    if unknown_timeouts:
        raise ProjectConfigError(f"unknown timeout role(s): {_names(unknown_timeouts)}")
    for role, value in timeouts.items():
        defaults[f"{role}_timeout"] = _positive_int(value, f"run.timeouts.{role}")
    return defaults


def _flatten_role_table(
    defaults: dict[str, Any],
    dest_prefix: str,
    name: str,
    values: dict[str, Any],
) -> None:
    """Flatten one role (or role tier) table into `<prefix>_agent` / `<prefix>_model`."""
    unknown_role_keys = set(values) - {"agent", "model"}
    if unknown_role_keys:
        raise ProjectConfigError(f"unknown [{name}] key(s): {_names(unknown_role_keys)}")
    if "agent" in values:
        defaults[f"{dest_prefix}_agent"] = _choice(
            values["agent"], f"{name}.agent", _AGENT_CHOICES
        )
    if "model" in values:
        defaults[f"{dest_prefix}_model"] = _string(values["model"], f"{name}.model")


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectConfigError(f"{name} must be a non-empty string")
    return value


def _choice(value: Any, name: str, choices: set[str]) -> str:
    result = _string(value, name)
    if result not in choices:
        raise ProjectConfigError(f"{name} must be one of: {_names(choices)}")
    return result


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProjectConfigError(f"{name} must be an integer of at least 1")
    if name.endswith("max_rounds") and value > MAX_ROUNDS:
        raise ProjectConfigError(f"{name} must be at most {MAX_ROUNDS}")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProjectConfigError(f"{name} must be an integer of 0 or more")
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
