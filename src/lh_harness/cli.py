from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import stat
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable

from . import HOMEPAGE, ISSUES_URL, __version__
from .config import (
    PROJECT_CONFIG_PATH,
    ProjectConfigError,
    create_project_config,
    load_run_defaults,
)
from .types import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_CODEX_MODEL,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_WORKSPACE_PATH,
    MAX_ROUNDS,
    EpisodeBudget,
    HarnessConfig,
)
from .utils.agent_cli import probe_agent_cli
from .supervisor.control_bus import (
    _append_jsonl as _append_jsonl_nofollow,
    _atomic_bytes_write,
    _ensure_dir_nofollow,
    _open_nofollow,
)

if TYPE_CHECKING:
    from .utils import UpdateCheckResult

_EPILOG = f"Homepage: {HOMEPAGE}\nFound a bug? Please open an issue: {ISSUES_URL}"

# Runs are project-scoped.
_DEFAULT_RUNS_ROOT = "./.lh-harness/runs"
_DEFAULT_MAX_ROUNDS = DEFAULT_MAX_ROUNDS
_MAX_TASK_FILE_BYTES = 100_000

# Agent backends as (choice, CLI binary, default model).
_AGENTS = (
    ("claude_code", "claude", DEFAULT_CLAUDE_MODEL),
    ("codex", "codex", DEFAULT_CODEX_MODEL),
)
_AGENT_CHOICES = tuple(name for name, _, _ in _AGENTS)
# Each agent reads MCP config in its own format, so each gets its own flag.
_MCP_CONFIG_DESTS = {"claude_code": "claude_mcp_config", "codex": "codex_mcp_config"}

# Computer-use plugins, listed here so `--help` needs no plugins import.
# Kept in sync by a check in `_plugin_command`.
_CODEX_GUI_PLUGIN = "codex-computer-use"
_PLUGIN_CHOICES = (_CODEX_GUI_PLUGIN, "open-computer-use", "clawdcursor")

# Role options as (dest prefix, broader option it falls back to, help scope).
# Each entry gets a matching `--<role>-agent` and `--<role>-model` flag;
# resolution walks the fallback chain and ends at the global --agent / --model.
_ROLE_OPTIONS = (
    ("manager", None, "the scheduler role"),
    ("executor", None, "both executor roles"),
    ("gui_executor", "executor", "GUI/visual subtasks"),
    ("cli_executor", "executor", "CLI/non-GUI subtasks"),
    ("auditor", None, "both auditor roles"),
    ("gui_auditor", "auditor", "GUI audit"),
    ("cli_auditor", "auditor", "CLI audit"),
    ("final_response", "manager", "the closing reply written for you"),
)
_ROLE_PARENTS = {role: parent for role, parent, _ in _ROLE_OPTIONS}
_ROLE_SCOPES = {role: scope for role, _, scope in _ROLE_OPTIONS}

# Per-role episode budgets as (dest prefix, timeout seconds). The
# executors get the long task timeout; the scheduler and auditors get the short one.
_BUDGET_OPTIONS = (
    ("manager", 300),
    ("gui_executor", 1800),
    ("cli_executor", 1800),
    ("auditor", 300),
)


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    """Append each option's default while keeping the epilog's own line breaks."""

    def _get_help_string(self, action: argparse.Action) -> str | None:
        if action.default is None or action.default == [] or action.nargs == 0:
            return action.help
        return super()._get_help_string(action)


def _flag(prefix: str, suffix: str) -> str:
    return f"--{prefix.replace('_', '-')}-{suffix}"


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    # Only the run-count option uses this helper with a dedicated wrapper
    # below; episode timeout values remain positive without inheriting a
    # surprisingly small round ceiling.
    return parsed


def _max_rounds(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > MAX_ROUNDS:
        raise argparse.ArgumentTypeError(f"must be at most {MAX_ROUNDS}")
    return parsed


def _port(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("must be between 0 and 65535")
    return parsed


def _reserve_run_dir(runs_root: str | Path, requested_run_id: str | None) -> tuple[str, Path]:
    """Reserve an isolated run directory without ever reusing old state."""

    root = Path(runs_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    explicit = requested_run_id is not None
    candidate = str(requested_run_id or "").strip()
    if explicit and (
        not candidate
        or candidate in {".", ".."}
        or "/" in candidate
        or "\\" in candidate
        or "\x00" in candidate
    ):
        raise ValueError("run id must be a non-empty single path component")

    for _ in range(8):
        run_id = candidate if explicit else f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:8]}"
        run_dir = root / run_id
        try:
            resolved = run_dir.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("run id resolves outside the configured runs root") from exc
        try:
            run_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            if explicit:
                raise ValueError(f"run already exists: {run_id}") from None
            continue
        return run_id, run_dir
    raise ValueError("could not reserve a unique run id")


def _read_supervised_record(
    path: Path,
    *,
    unavailable_message: str,
    invalid_message: str,
    too_large_message: str,
) -> dict[str, object]:
    """Read supervisor-owned metadata without following any path symlink."""

    fd: int | None = None
    try:
        fd = _open_nofollow(path)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(invalid_message)
        if metadata.st_size > 256 * 1024:
            raise ValueError(too_large_message)
        raw = bytearray()
        remaining = 256 * 1024 + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            raw.extend(chunk)
            remaining -= len(chunk)
        if len(raw) > 256 * 1024:
            raise ValueError(too_large_message)
        value = json.loads(bytes(raw).decode("utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(unavailable_message) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(invalid_message) from exc
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(invalid_message) from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    if not isinstance(value, dict):
        raise ValueError(invalid_message)
    return value


def _adopt_supervised_run_dir(
    runs_root: str | Path,
    requested_run_id: str | None,
    *,
    task: str | None,
    agent: str,
    model: str | None,
    workspace: str | Path | None,
    max_rounds: int,
    role_configs: dict[str, dict[str, str | None]] | None = None,
) -> tuple[str, Path]:
    """Take over the reservation created by :class:`RunSupervisor`.

    The supervisor deliberately creates the run directory and writes an owner
    reservation *before* spawning the worker.  A normal CLI reservation would
    therefore reject the worker with ``run already exists``.  ``--supervised``
    is an internal capability, so it may only adopt a directory whose durable
    owner record proves that this exact child process was reserved for this
    request.  This also prevents a user from using the hidden flag to attach a
    random process to an arbitrary old run.
    """

    root = Path(runs_root).expanduser().resolve()
    if requested_run_id is None:
        raise ValueError("supervised runs require an explicit run id")
    candidate = str(requested_run_id).strip()
    if (
        not candidate
        or candidate in {".", ".."}
        or "/" in candidate
        or "\\" in candidate
        or "\x00" in candidate
    ):
        raise ValueError("run id must be a non-empty single path component")
    run_dir = root / candidate
    try:
        resolved = run_dir.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("run id resolves outside the configured runs root") from exc
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ValueError("supervised run reservation is missing")

    owner = _read_supervised_record(
        run_dir / "control" / "owner.json",
        unavailable_message="supervised run reservation is incomplete",
        invalid_message="supervised run reservation is invalid",
        too_large_message="supervised reservation metadata is too large",
    )
    status = _read_supervised_record(
        run_dir / "control" / "status.json",
        unavailable_message="supervised run reservation is incomplete",
        invalid_message="supervised run reservation is invalid",
        too_large_message="supervised reservation metadata is too large",
    )
    if str(owner.get("run_id") or "") != candidate:
        raise ValueError("supervised run reservation has the wrong run id")
    # The owner PID is assigned before Popen. Requiring the current process
    # identity closes the obvious arbitrary-adoption hole while still matching
    # the supervisor's child exactly.
    try:
        owner_pid = int(owner.get("pid", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("supervised run reservation has an invalid owner pid") from exc
    if owner_pid not in {0, os.getpid()}:
        raise ValueError("supervised run reservation belongs to another process")
    if owner_pid == 0:
        # There is a small, legitimate window between Popen() returning and
        # the supervisor promoting the reservation with the child PID. Bind
        # that window to the recorded parent supervisor, never to an arbitrary
        # caller that happens to know the run id.
        try:
            reservation_parent = int(owner.get("supervisor_pid", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("supervised run reservation has an invalid parent pid") from exc
        if reservation_parent <= 0 or reservation_parent != os.getppid():
            raise ValueError("supervised run reservation is not owned by this worker")
    if str(status.get("run_id") or candidate) != candidate:
        raise ValueError("supervised run status has the wrong run id")
    lifecycle = str(status.get("status") or owner.get("state") or "").strip().lower()
    if lifecycle in {"completed", "complete", "failed", "cancelled", "canceled", "blocked", "incomplete"}:
        raise ValueError("supervised run reservation is already terminal")

    expected_task = str(owner.get("task") or "").strip()
    if task is not None and expected_task and expected_task != task.strip():
        raise ValueError("supervised run task does not match its reservation")
    if str(owner.get("agent") or agent) != agent:
        raise ValueError("supervised run agent does not match its reservation")
    expected_model = owner.get("model")
    if (str(expected_model).strip() if expected_model is not None else None) != (model.strip() if isinstance(model, str) else model):
        raise ValueError("supervised run model does not match its reservation")
    expected_roles = owner.get("role_configs")
    expected_roles = expected_roles if isinstance(expected_roles, dict) and expected_roles else None
    if expected_roles != role_configs:
        raise ValueError("supervised run role configuration does not match its reservation")
    try:
        reserved_rounds = int(owner.get("max_rounds", max_rounds))
    except (TypeError, ValueError) as exc:
        raise ValueError("supervised run reservation has invalid max_rounds") from exc
    if reserved_rounds != max_rounds:
        raise ValueError("supervised run max_rounds does not match its reservation")
    if workspace is not None and owner.get("workspace"):
        try:
            requested_workspace = Path(workspace).expanduser().resolve(strict=False)
            reserved_workspace = Path(str(owner["workspace"])).expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError("supervised run workspace is invalid") from exc
        if requested_workspace != reserved_workspace:
            raise ValueError("supervised run workspace does not match its reservation")
    return candidate, run_dir


def _claim_supervised_owner(run_id: str, run_dir: Path) -> None:
    """Claim the pre-Popen reservation as soon as the worker starts."""

    control = run_dir / "control"
    owner_path = control / "owner.json"
    value = _read_supervised_record(
        owner_path,
        unavailable_message="supervised run owner metadata is unavailable",
        invalid_message="supervised run owner metadata is unavailable",
        too_large_message="supervised run owner metadata is too large",
    )
    if str(value.get("run_id") or "") != run_id:
        raise ValueError("supervised run owner metadata is invalid")
    try:
        existing_pid = int(value.get("pid", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("supervised run owner pid is invalid") from exc
    if existing_pid not in {0, os.getpid()}:
        raise ValueError("supervised run owner belongs to another process")
    if existing_pid == os.getpid():
        return
    try:
        parent_pid = int(value.get("supervisor_pid", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("supervised run parent pid is invalid") from exc
    if parent_pid != os.getppid():
        raise ValueError("supervised run parent does not match its reservation")
    from .supervisor.control_bus import ControlBus

    ControlBus(run_dir).write_owner({
        **value,
        "state": "running",
        "pid": os.getpid(),
        "pgid": os.getpid(),
        "signal_mode": "pgid",
    })


def _fallback_hint(role: str, suffix: str) -> str:
    parent = _ROLE_PARENTS[role]
    chain = ([_flag(parent, suffix)] if parent else []) + [f"--{suffix}"]
    return ", then ".join(chain)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    run_defaults: dict[str, object] = {}
    config_error: ProjectConfigError | None = None
    if raw_argv[:1] == ["run"]:
        try:
            run_defaults = load_run_defaults()
        except ProjectConfigError as exc:
            config_error = exc

    def run_default(name: str, fallback=None):
        return run_defaults.get(name, fallback)

    parser = argparse.ArgumentParser(
        prog="lh-harness",
        description=f"LongHorizon-Harness {__version__}",
        epilog=_EPILOG,
        formatter_class=_HelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"lh-harness {__version__}")
    sub = parser.add_subparsers(dest="command")

    def add_command(name: str, help_text: str) -> argparse.ArgumentParser:
        # Every subcommand repeats the homepage/issues epilog so the links show
        # up no matter which --help the user reaches for.
        return sub.add_parser(
            name,
            help=help_text,
            epilog=_EPILOG,
            formatter_class=_HelpFormatter,
        )

    run_parser = add_command("run", "Run a long-horizon task through role-managed LongHorizon-Harness")
    run_parser.add_argument("--task", required=True, help="Task text or @path")
    run_parser.add_argument(
        "--agent",
        default=run_default("agent", "codex"),
        choices=_AGENT_CHOICES,
        help="Agent implementation for every role.",
    )
    run_parser.add_argument(
        "--model",
        default=run_default("model"),
        help="Model for every role. Defaults to the chosen agent's own default.",
    )
    for role, _, scope in _ROLE_OPTIONS:
        run_parser.add_argument(
            _flag(role, "agent"),
            default=run_default(f"{role}_agent"),
            choices=_AGENT_CHOICES,
            help=f"Agent implementation for {scope}; defaults to {_fallback_hint(role, 'agent')}.",
        )
        run_parser.add_argument(
            _flag(role, "model"),
            default=run_default(f"{role}_model"),
            help=f"Model for {scope}; defaults to {_fallback_hint(role, 'model')}.",
        )
    # Only the local backend is implemented; kept as a flag so existing
    # `--env local` invocations keep working and future backends can slot in.
    run_parser.add_argument(
        "--env",
        default=run_default("env", "local"),
        choices=("local",),
        help="Environment the agent runs in.",
    )
    run_parser.add_argument(
        "--runs-root",
        default=run_default("runs_root", _DEFAULT_RUNS_ROOT),
        help="Base directory holding one isolated subfolder per run.",
    )
    run_parser.add_argument(
        "--run-id",
        default=None,
        help="Unique id for this run. Defaults to a timestamp + short uuid. All run data goes under <runs-root>/<run-id>/.",
    )
    run_parser.add_argument(
        "--workspace",
        default=run_default("workspace"),
        help="Override the working directory the agents operate in. Defaults to the directory lh-harness was started from.",
    )
    run_parser.add_argument(
        "--harness-dir",
        default=run_default("harness_dir"),
        help="Override the harness state directory. Defaults to <runs-root>/<run-id>/harness.",
    )
    run_parser.add_argument(
        "--log-dir",
        default=run_default("log_dir"),
        help="Override the log directory. Defaults to <runs-root>/<run-id>/lh_harness.",
    )
    # Credentials are handed to the agent CLI as its own env vars; each adapter
    # maps them to its backend (ANTHROPIC_* for claude_code, OPENAI_* plus a
    # provider override for codex).
    run_parser.add_argument(
        "--api-key",
        help="LLM API key for the agent CLI. Omit to reuse the CLI's own login (`claude login` / `codex login`).",
    )
    run_parser.add_argument(
        "--base-url",
        default=run_default("base_url"),
        help="OpenAI-compatible endpoint for the agent CLI. The trailing `/v1` is added or stripped per backend.",
    )
    run_parser.add_argument(
        "--prompt-language",
        choices=("en", "zh"),
        default=run_default("prompt_language", "en"),
        help="Language for manager/executor/auditor prompts.",
    )
    # One entry per agent, each in that agent's own format: no translation.
    run_parser.add_argument(
        "--claude-mcp-config",
        default=run_default("claude_mcp_config"),
        help="MCP config for Claude Code, in its own `.mcp.json` format. Overrides the installed "
        "computer-use plugin, which is loaded automatically otherwise.",
    )
    run_parser.add_argument(
        "--codex-mcp-config",
        default=run_default("codex_mcp_config"),
        help="MCP config for Codex, a TOML file holding `[mcp_servers.<name>]` tables. Overrides "
        "the installed computer-use plugin, which is loaded automatically otherwise.",
    )
    run_parser.add_argument(
        "--mcp-add-dir",
        action="append",
        default=None,
        help="Extra directory to expose to the agent. May be repeated.",
    )
    run_parser.add_argument(
        "--max-rounds",
        type=_max_rounds,
        default=run_default("max_rounds"),
        help=f"Maximum number of manage-execute-audit rounds (1-{MAX_ROUNDS}). If omitted, uses {_DEFAULT_MAX_ROUNDS}.",
    )
    for role, timeout in _BUDGET_OPTIONS:
        scope = _ROLE_SCOPES[role]
        run_parser.add_argument(
            _flag(role, "timeout"),
            type=_positive_int,
            default=run_default(f"{role}_timeout", timeout),
            help=f"Per-episode timeout in seconds for {scope}.",
        )
    run_parser.add_argument(
        "--dashboard",
        action=argparse.BooleanOptionalAction,
        default=run_default("dashboard", True),
        help="Launch the web dashboard in the background for live monitoring and human approval.",
    )
    run_parser.add_argument(
        "--dashboard-port",
        type=_port,
        default=run_default("dashboard_port", 0),
        help="Dashboard port; 0 lets the OS pick a free one.",
    )
    run_parser.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
        help="Dashboard bind host (default: 127.0.0.1).",
    )
    run_parser.add_argument(
        "--dashboard-no-open",
        action="store_true",
        help="Do not open the dashboard URL in a browser.",
    )
    run_parser.add_argument(
        "--dashboard-auth-token",
        default=os.environ.get("LH_HARNESS_WEB_TOKEN"),
        help="Bearer token when binding the live dashboard beyond localhost.",
    )
    run_parser.add_argument(
        "--keep-dashboard",
        action="store_true",
        help="Keep the dashboard alive after any run; Dashboard Stop/Abort already keeps it alive.",
    )
    run_parser.add_argument(
        "--supervised",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    dash_parser = add_command(
        "dashboard",
        "Serve the Web workbench",
    )
    dash_parser.add_argument(
        "--runs-root",
        default=_DEFAULT_RUNS_ROOT,
        help="Base directory holding runs; the UI lists all runs and lets you switch between them.",
    )
    dash_parser.add_argument(
        "--log-dir",
        default=None,
        help="Pin one run's log directory instead of browsing --runs-root.",
    )
    dash_parser.add_argument(
        "--workspace-root",
        default=".",
        help="Default workspace for runs created from the Web workbench (default: current directory).",
    )
    dash_parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    dash_parser.add_argument("--port", type=_port, default=8799, help="Dashboard port; 0 lets the OS pick a free one.")
    dash_parser.add_argument("--no-open", action="store_true", help="Do not open the dashboard URL in a browser.")
    dash_parser.add_argument(
        "--auth-token",
        default=os.environ.get("LH_HARNESS_WEB_TOKEN"),
        help="Bearer token for remote dashboard/API access (also LH_HARNESS_WEB_TOKEN).",
    )

    web_parser = add_command("web", "Serve the FastAPI Web control API")
    web_parser.add_argument("--runs-root", default=_DEFAULT_RUNS_ROOT, help="Base directory holding runs.")
    web_parser.add_argument(
        "--workspace-root",
        default=".",
        help="Default workspace for runs created from the Web workbench (default: current directory).",
    )
    web_parser.add_argument("--log-dir", default=None, help="Pin one run's log directory instead of browsing runs.")
    web_parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    web_parser.add_argument("--port", type=_port, default=8799, help="API port; 0 lets the OS pick a free one.")
    web_parser.add_argument("--no-open", action="store_true", help="Do not open the API URL in a browser.")
    web_parser.add_argument(
        "--auth-token",
        default=os.environ.get("LH_HARNESS_WEB_TOKEN"),
        help="Bearer token for remote/API access (also LH_HARNESS_WEB_TOKEN).",
    )

    add_command("doctor", "Check the local environment and report computer-use plugin state")

    plugin_parser = add_command("plugin", "Install or remove computer-use plugins")
    plugin_actions = plugin_parser.add_subparsers(dest="plugin_command")
    for action, help_text in (
        ("list", "Show the available computer-use plugins and their install state"),
        ("install", "Install a computer-use plugin and register it with an agent"),
        ("uninstall", "Remove a computer-use plugin"),
    ):
        sub_parser = plugin_actions.add_parser(
            action,
            help=help_text,
            epilog=_EPILOG,
            formatter_class=_HelpFormatter,
        )
        if action == "list":
            continue
        sub_parser.add_argument(
            "name",
            choices=_PLUGIN_CHOICES,
            help="Plugin to set up. Run `lh-harness plugin list` for what each one provides.",
        )
        if action == "install":
            sub_parser.add_argument(
                "--agent",
                action="append",
                choices=_AGENT_CHOICES,
                default=None,
                help="Agent to register the plugin with. May be repeated; defaults to every supported agent.",
            )
            sub_parser.add_argument(
                "--no-activate",
                action="store_true",
                help="Skip the plugin's consent and OS-permission commands (they need a desktop session).",
            )

    init_parser = add_command("init", "Generate ./.lh-harness/config.toml for this project")
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing project configuration file.",
    )

    add_command("check-update", "Check PyPI for a newer LongHorizon-Harness release")

    args = parser.parse_args(raw_argv)
    if args.command == "run":
        if config_error is not None:
            parser.error(str(config_error))
        if args.mcp_add_dir is None:
            args.mcp_add_dir = list(run_defaults.get("mcp_add_dir", []))
        if PROJECT_CONFIG_PATH.is_file():
            print(f"Using config: {PROJECT_CONFIG_PATH.resolve()}")
        return _run_command(args)
    if args.command == "dashboard":
        return _dashboard_command(args)
    if args.command == "web":
        return _web_command(args)
    if args.command == "doctor":
        return _doctor_command()
    if args.command == "plugin":
        if not args.plugin_command:
            plugin_parser.print_help()
            return 2
        return _plugin_command(args)
    if args.command == "init":
        return _init_command(args)
    if args.command == "check-update":
        return _check_update_command()

    parser.print_help()
    return 2


def _doctor_command() -> int:
    from .utils import start_update_check

    update_check = start_update_check(__version__)
    print(f"LongHorizon-Harness doctor ({__version__})")
    print(f"Platform: {platform.platform()}")
    print(f"Homepage: {HOMEPAGE}")
    print(f"Issues:   {ISSUES_URL}")

    failures = 0
    warnings = 0
    python_ok = sys.version_info >= (3, 10)
    _doctor_line(
        "OK" if python_ok else "FAIL",
        "Python",
        f"{platform.python_version()} ({sys.executable})",
    )
    if not python_ok:
        failures += 1

    if PROJECT_CONFIG_PATH.is_file():
        try:
            defaults = load_run_defaults()
        except ProjectConfigError as exc:
            _doctor_line("FAIL", "Project config", str(exc))
            failures += 1
        else:
            _doctor_line(
                "OK",
                "Project config",
                f"{PROJECT_CONFIG_PATH.resolve()} ({len(defaults)} configured default(s))",
            )
    else:
        _doctor_line("SKIP", "Project config", f"{PROJECT_CONFIG_PATH} does not exist")

    found_agents: dict[str, str] = {}
    for name, binary, _ in _AGENTS:
        cli = probe_agent_cli(binary)
        if not cli.found:
            _doctor_line("WARN", name, cli.problem)
            warnings += 1
            continue
        if not cli.usable:
            # On PATH but not runnable is worse than absent: it silently breaks
            # every run, so it is a failure rather than a warning.
            _doctor_line("FAIL", name, cli.problem)
            failures += 1
            continue
        found_agents[name] = cli.path
        _doctor_line("OK", name, f"{cli.version} ({cli.path})")

    if not found_agents:
        _doctor_line("FAIL", "Agent runtime", "install Claude Code or Codex CLI and add it to PATH")
        failures += 1

    warnings += _doctor_node_toolchain()

    warnings += _doctor_plugin_state(codex_path=found_agents.get("codex"))

    update_result = update_check.result(timeout=3.0)
    update_warning = _report_update_result(update_result)
    warnings += int(update_warning)

    if failures:
        summary = f"{failures} required check(s) failed"
    elif warnings:
        summary = f"ready with {warnings} warning(s)"
    else:
        summary = "ready"
    print(f"Doctor result: {summary}")
    return 0 if failures == 0 else 1


def _doctor_line(status: str, label: str, detail: str) -> None:
    print(f"[{status:<4}] {label}: {detail}")


_MIN_NODE_MAJOR = 20


def _doctor_node_toolchain() -> int:
    """Report the Node/npm toolchain used by npm plugins."""
    from .plugins import node_version, npm_binary, npm_version

    warnings = 0
    npm = npm_binary()
    if not npm:
        _doctor_line(
            "WARN",
            "npm",
            "not found on PATH; `lh-harness plugin install` needs Node.js 20+ (https://nodejs.org)",
        )
        warnings += 1
    else:
        _doctor_line("OK", "npm", f"{npm_version() or 'unknown version'} ({npm})")

    node = node_version()
    if not node:
        _doctor_line(
            "WARN",
            "Node.js",
            "`node --version` was unreadable; npm plugins need Node.js 20+",
        )
        return warnings + 1
    major_match = re.match(r"(\d+)", node)
    major = int(major_match.group(1)) if major_match else 0
    if major >= _MIN_NODE_MAJOR:
        _doctor_line("OK", "Node.js", node)
    else:
        _doctor_line(
            "WARN",
            "Node.js",
            f"{node} is older than {_MIN_NODE_MAJOR}; npm plugins may fail",
        )
        warnings += 1
    return warnings


def _doctor_plugin_state(*, codex_path: str | None) -> int:
    """Report each computer-use plugin's state, in `plugin list` order."""
    from .plugins import (
        COMMUNITY_PLUGINS,
        COMPUTER_USE_PLUGIN_ID,
        PluginError,
        codex_gui_grants,
        community_plugin_activation,
        community_plugin_state,
        get_codex_plugin_state,
        global_registrations,
        npm_binary,
    )

    hint = "run `lh-harness plugin install {name}`"
    warnings = 0

    if not codex_path:
        _doctor_line("SKIP", _CODEX_GUI_PLUGIN, "skipped while the Codex CLI is unusable")
    else:
        try:
            state = get_codex_plugin_state(COMPUTER_USE_PLUGIN_ID, codex_binary=codex_path)
        except PluginError as exc:
            _doctor_line("WARN", _CODEX_GUI_PLUGIN, str(exc))
            warnings += 1
        else:
            version = f" {state.version}" if state.version else ""
            if state.ready:
                _doctor_line("OK", _CODEX_GUI_PLUGIN, f"{state.plugin_id}{version} is enabled")
                _doctor_line("NOTE", f"{_CODEX_GUI_PLUGIN} grants", codex_gui_grants())
            elif state.available:
                detail = "installed but disabled" if state.installed else "not installed"
                _doctor_line(
                    "SKIP",
                    _CODEX_GUI_PLUGIN,
                    f"{state.plugin_id} is {detail}; " + hint.format(name=_CODEX_GUI_PLUGIN),
                )
            else:
                _doctor_line(
                    "WARN", _CODEX_GUI_PLUGIN, f"{state.plugin_id} is unavailable; update Codex CLI"
                )
                warnings += 1

    if not npm_binary():
        for plugin in COMMUNITY_PLUGINS:
            _doctor_line("SKIP", plugin.plugin_id, "state unknown while npm is missing")
        return warnings

    for plugin in COMMUNITY_PLUGINS:
        if not plugin.supports_platform(sys.platform):
            _doctor_line("SKIP", plugin.plugin_id, f"does not support {sys.platform}")
            continue
        try:
            state = community_plugin_state(plugin)
        except PluginError as exc:
            _doctor_line("WARN", plugin.plugin_id, str(exc))
            warnings += 1
            continue
        if not state.installed:
            _doctor_line(
                "SKIP", plugin.plugin_id, "not installed; " + hint.format(name=plugin.plugin_id)
            )
            continue
        _doctor_line("OK", plugin.plugin_id, f"{plugin.package} {state.version}".strip())
        ready, detail = community_plugin_activation(plugin)
        if ready is False:
            _doctor_line("WARN", f"{plugin.plugin_id} grants", detail)
            warnings += 1
        elif ready:
            _doctor_line("OK", f"{plugin.plugin_id} grants", detail)
        leftovers = global_registrations(plugin)
        if leftovers:
            _doctor_line(
                "WARN",
                f"{plugin.plugin_id} scope",
                f"also registered globally in {', '.join(sorted(set(leftovers)))}; "
                "the harness loads it per run, so remove the global entry to keep GUI "
                "control out of unrelated sessions",
            )
            warnings += 1
    return warnings + _doctor_active_plugins()


def _doctor_active_plugins() -> int:
    """Report which plugin each agent will load, following the priority order."""
    from .plugins import PluginError, active_plugin_for_agent

    warnings = 0
    for agent in _AGENT_CHOICES:
        try:
            active = active_plugin_for_agent(agent)
        except PluginError as exc:
            _doctor_line("WARN", f"Computer use ({agent})", str(exc))
            warnings += 1
            continue
        if active is None:
            _doctor_line(
                "SKIP",
                f"Computer use ({agent})",
                "no plugin installed; GUI subtasks will have no computer-use server",
            )
            continue
        plugin_id, config = active
        _doctor_line(
            "OK",
            f"Computer use ({agent})",
            f"{plugin_id} ({config or 'loaded natively by the agent'})",
        )
    return warnings


def _plugin_command(args: argparse.Namespace) -> int:
    from .plugins import PluginError, community_plugin_ids

    assert set(_PLUGIN_CHOICES) == {_CODEX_GUI_PLUGIN, *community_plugin_ids()}, (
        "CLI plugin choices are stale"
    )

    if args.plugin_command == "list":
        return _plugin_list_command()
    try:
        if args.name == _CODEX_GUI_PLUGIN:
            return _codex_gui_plugin_command(args)
        return _community_plugin_command(args)
    except PluginError as exc:
        _doctor_line("FAIL", args.name, str(exc))
        return 1


def _plugin_list_command() -> int:
    from .plugins import (
        COMMUNITY_PLUGINS,
        COMPUTER_USE_PLUGIN_ID,
        PLUGIN_PRIORITY,
        PluginError,
        active_plugin_for_agent,
        codex_gui_grants,
        community_plugin_activation,
        community_plugin_state,
        get_codex_plugin_state,
        npm_binary,
        plugins_root,
    )

    def entry(name: str, summary: str, fields: dict[str, str]) -> None:
        print(f"\n{name}")
        print(f"  {summary}")
        width = max(len(key) for key in fields)
        for key, value in fields.items():
            print(f"  {key.ljust(width)} : {value}")

    print(f"Priority when several are installed: {' > '.join(PLUGIN_PRIORITY)}")
    print(f"Generated MCP configs live under {plugins_root()}")
    for agent in _AGENT_CHOICES:
        try:
            active = active_plugin_for_agent(agent)
        except PluginError as exc:
            print(f"Active for {agent}: unknown ({exc})")
            continue
        print(f"Active for {agent}: {active[0] if active else 'none installed'}")

    codex_cli = probe_agent_cli("codex")
    if not codex_cli.found:
        codex_state = "unknown (Codex CLI is not installed)"
    elif not codex_cli.usable:
        codex_state = f"unknown ({codex_cli.problem})"
    else:
        try:
            official = get_codex_plugin_state(COMPUTER_USE_PLUGIN_ID, codex_binary=codex_cli.path)
        except PluginError as exc:
            codex_state = f"unknown ({exc})"
        else:
            if official.ready:
                codex_state = f"installed and enabled {official.version}".strip()
            elif official.installed:
                codex_state = "installed but disabled"
            elif official.available:
                codex_state = "not installed"
            else:
                codex_state = f"not offered by this Codex build on {sys.platform}"
    entry(
        _CODEX_GUI_PLUGIN,
        "Official Codex Computer Use plugin, bundled with the Codex CLI.",
        {
            "source": f"codex plugin ({COMPUTER_USE_PLUGIN_ID})",
            "agents": "codex",
            "platforms": "whatever your Codex build offers",
            "grants": codex_gui_grants(),
            "homepage": "https://github.com/openai/codex",
            "state": codex_state,
        },
    )

    npm_missing = not npm_binary()
    for plugin in COMMUNITY_PLUGINS:
        grants = "not checked"
        if npm_missing:
            state = "unknown (npm is not on PATH; needs Node.js 20 or later)"
        else:
            try:
                package_state = community_plugin_state(plugin)
            except PluginError as exc:
                state = f"unknown ({exc})"
            else:
                state = (
                    f"installed {package_state.version}".strip()
                    if package_state.installed
                    else "not installed"
                )
                if package_state.installed:
                    ready, detail = community_plugin_activation(plugin)
                    prefix = {True: "granted", False: "MISSING", None: "unknown"}[ready]
                    grants = f"{prefix}: {detail}"
        platforms = ", ".join(sorted(plugin.platforms))
        if not plugin.supports_platform(sys.platform):
            platforms += f" (not {sys.platform})"
        entry(
            plugin.plugin_id,
            plugin.summary,
            {
                "source": f"npm ({plugin.package})",
                "agents": ", ".join(sorted(plugin.agents)),
                "platforms": platforms,
                "grants": grants,
                "homepage": plugin.homepage,
                "state": state,
            },
        )
    return 0


def _codex_gui_plugin_command(args: argparse.Namespace) -> int:
    from .plugins import install_computer_use_plugin, uninstall_computer_use_plugin

    codex = probe_agent_cli("codex")
    if not codex.found:
        _doctor_line("FAIL", _CODEX_GUI_PLUGIN, "Codex CLI is required; install it and retry")
        return 1
    if not codex.usable:
        _doctor_line("FAIL", _CODEX_GUI_PLUGIN, codex.problem)
        return 1
    codex_path = codex.path
    if args.plugin_command == "install" and args.agent and set(args.agent) - {"codex"}:
        _doctor_line("FAIL", _CODEX_GUI_PLUGIN, "this plugin only supports --agent codex")
        return 1

    def report(status: str, message: str) -> None:
        _doctor_line(status.upper(), _CODEX_GUI_PLUGIN, message)

    if args.plugin_command == "install":
        state = install_computer_use_plugin(
            codex_binary=codex_path, on_status=report, activate=not args.no_activate
        )
        version = f" {state.version}" if state.version else ""
        report("ok", f"{state.plugin_id}{version} is installed and enabled")
    else:
        state = uninstall_computer_use_plugin(codex_binary=codex_path, on_status=report)
        report("ok", f"{state.plugin_id} is not installed")
    return 0


def _community_plugin_command(args: argparse.Namespace) -> int:
    from .plugins import (
        get_community_plugin,
        install_community_plugin,
        node_version,
        uninstall_community_plugin,
    )

    plugin = get_community_plugin(args.name)

    def report(status: str, message: str) -> None:
        _doctor_line(status.upper(), plugin.plugin_id, message)

    if args.plugin_command != "install":
        uninstall_community_plugin(plugin, on_status=report)
        return 0

    requested = args.agent or sorted(plugin.agents)
    explicit = bool(args.agent)
    agents: list[str] = []
    missing: list[tuple[str, str]] = []
    for agent in requested:
        binary = next(b for n, b, _ in _AGENTS if n == agent)
        cli = probe_agent_cli(binary)
        if cli.usable:
            agents.append(agent)
        else:
            missing.append((agent, cli.problem))
    for agent, problem in missing:
        # Writing config for an agent that cannot run would produce a plugin
        # nothing reads. Only an explicit --agent makes this an error.
        level = "FAIL" if explicit else "SKIP"
        suffix = "" if explicit else "; skipped"
        _doctor_line(level, f"Agent ({agent})", f"{problem}{suffix}")
    if explicit and missing:
        return 1
    if not agents:
        _doctor_line("FAIL", "Agent", "install Claude Code or Codex CLI, then retry")
        return 1

    print(f"Installing {plugin.plugin_id} for: {', '.join(agents)}")
    if not node_version():
        _doctor_line("WARN", "Node.js", "could not read `node --version`; the plugin may not run")
    install_community_plugin(
        plugin,
        agents=agents,
        on_status=report,
        activate=not args.no_activate,
    )
    return 0


def _init_command(args: argparse.Namespace) -> int:
    try:
        path = create_project_config(force=args.force)
    except FileExistsError as exc:
        print(f"Config already exists: {Path(exc.args[0]).resolve()}", file=sys.stderr)
        print("Use `lh-harness init --force` to replace it.", file=sys.stderr)
        return 1
    print(f"Created config: {path.resolve()}")
    return 0


def _check_update_command() -> int:
    from .utils import check_for_update

    result = check_for_update(__version__)
    _report_update_result(result)
    return 1 if result.status == "failed" else 0


def _report_update_result(result: UpdateCheckResult | None) -> bool:
    from .utils import PYPI_PROJECT_URL

    if result is None or result.status == "failed":
        _doctor_line(
            "WARN",
            "Update",
            f"automatic update check failed; check manually: {PYPI_PROJECT_URL}",
        )
        return True
    if result.status == "update_available":
        _doctor_line(
            "WARN",
            "Update",
            f"{result.latest_version} is available (installed: {result.current_version}); {PYPI_PROJECT_URL}",
        )
        return True
    _doctor_line("OK", "Update", f"{result.current_version} is the latest version")
    return False


def _load_web_server_runner():
    """Import the Web server, keeping the cost off CLI-only invocations."""

    from .webapi.server import run_web_server

    # server.py imports Uvicorn lazily. Check it before opening the browser so
    # a broken install cannot send the user to a dead endpoint.
    __import__("uvicorn")
    return run_web_server


def _broken_install_message(exc: ImportError) -> str:
    return (
        "Web dependencies are missing from this installation. "
        "Reinstall with `pip install --force-reinstall lh-harness`. "
        f"({exc})"
    )


def _dashboard_command(args: argparse.Namespace) -> int:
    """Run the React/FastAPI workbench."""

    try:
        run_web_server = _load_web_server_runner()
    except ImportError as exc:
        print(_broken_install_message(exc), file=sys.stderr)
        return 2
    return _serve_web_workbench(args, run_web_server)


def _web_command(args: argparse.Namespace) -> int:
    """Run the FastAPI/WebSocket control plane in the foreground."""

    try:
        run_web_server = _load_web_server_runner()
    except ImportError as exc:
        print(_broken_install_message(exc), file=sys.stderr)
        return 2
    return _serve_web_workbench(args, run_web_server)


def _serve_web_workbench(args: argparse.Namespace, run_web_server) -> int:
    """Apply shared CLI policy and run the Web workbench in the foreground."""

    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.auth_token:
        print(
            "Refusing remote Web control API without --auth-token or "
            "LH_HARNESS_WEB_TOKEN.",
            file=sys.stderr,
        )
        return 2
    url = f"http://127.0.0.1:{args.port}/" if args.host in {"0.0.0.0", "::"} else f"http://{args.host}:{args.port}/"
    if not args.no_open and args.port:
        # ``run_web_server`` blocks in Uvicorn. Open from a waiter thread, but
        # only after the root page actually responds; opening before bind/start
        # races the browser into a connection-error page on slower machines.
        threading.Thread(
            target=_open_browser_when_ready,
            args=(url,),
            name="lh-harness-browser-opener",
            daemon=True,
        ).start()
    elif not args.no_open:
        print("API port is assigned by the OS; automatic browser opening is disabled until the port is known.")
    try:
        return run_web_server(
            runs_root=args.runs_root,
            log_dir=args.log_dir,
            host=args.host,
            port=args.port,
            workspace_root=args.workspace_root,
            auth_token=args.auth_token,
        )
    except (KeyboardInterrupt, ValueError) as exc:
        if isinstance(exc, ValueError):
            print(str(exc), file=sys.stderr)
            return 2
        print("Web API stopped.")
        return 0


def _open_browser(url: str) -> None:
    try:
        opened = webbrowser.open(url, new=2)
        if not opened:
            print(f"Open this URL in a browser: {url}")
    except Exception as exc:  # browser integration must never break a run
        print(f"Could not open browser automatically: {exc}; open {url}", file=sys.stderr)


def _wait_for_dashboard_ready(url: str, *, timeout: float = 10.0, poll_interval: float = 0.05) -> bool:
    """Return only after the Dashboard root page accepts an HTTP request."""

    from urllib.error import HTTPError, URLError
    from urllib.request import urlopen

    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        remaining = max(0.01, deadline - time.monotonic())
        try:
            with urlopen(url, timeout=min(0.5, remaining)) as response:  # noqa: S310 - caller supplies the local Dashboard URL
                if 200 <= int(response.status) < 500:
                    return True
        except HTTPError as exc:
            # Authentication/other client errors still prove the HTTP server
            # is ready; the browser can render the corresponding UI response.
            if 400 <= exc.code < 500:
                return True
        except (OSError, TimeoutError, URLError):
            pass
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
    return False


def _open_browser_when_ready(url: str) -> None:
    if _wait_for_dashboard_ready(url):
        _open_browser(url)
        return
    print(f"Dashboard did not become ready in time; open {url} manually.", file=sys.stderr)


def _write_dashboard_endpoint(path: Path, handle: object, *, run_id: str, log_dir: str) -> None:
    """Write best-effort endpoint discovery metadata next to a run."""
    try:
        endpoint = {
            "service": "lh-harness",
            "run_id": run_id,
            "url": str(getattr(handle, "url")),
            "host": str(getattr(handle, "host", "127.0.0.1")),
            "port": int(getattr(handle, "port")),
            "pid": os.getpid(),
            "log_dir": str(Path(log_dir).resolve()),
            "started_at": time.time(),
        }
        _atomic_bytes_write(
            path,
            (json.dumps(endpoint, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"Warning: could not write dashboard endpoint file: {exc}", file=sys.stderr)


def _finalize_embedded_supervisor(
    supervisor: object | None,
    run_id: str,
    *,
    report: dict[str, object] | None = None,
    returncode: int | None = 0,
    reason: str = "",
) -> None:
    """Best-effort durable owner cleanup for an in-process dashboard.

    Dashboard cleanup must never hide the manager's real result, so startup or
    shutdown errors are reported to stderr while the run itself remains the
    source of truth.
    """

    if supervisor is None:
        return
    try:
        finalize = getattr(supervisor, "finalize_attached_run")
        finalize(run_id, report=report, returncode=returncode, reason=reason)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"Warning: could not finalize embedded dashboard owner: {exc}", file=sys.stderr)


def _should_keep_embedded_dashboard(
    run_dir: Path,
    *,
    explicitly_requested: bool,
    report: dict[str, object] | None,
) -> bool:
    """Separate stopping a run from shutting down its embedded Web server."""

    if explicitly_requested and report is not None:
        return True
    try:
        from .supervisor.control_bus import ControlBus

        requested_action = str(ControlBus(run_dir).read_status().get("requested_action") or "").strip().lower()
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return requested_action in {"stop", "abort", "cancel"}


async def _run_with_attached_control(
    worker: Awaitable[dict[str, object]],
    *,
    run_dir: Path,
    enabled: bool,
    poll_interval: float = 0.1,
) -> dict[str, object]:
    """Cancel an embedded Manager task from its durable lifecycle request.

    The FastAPI server runs in a background thread of the same process as the
    Manager. Its Stop/Abort routes therefore persist an intent instead of
    signalling the hosting PID. This watcher crosses that thread boundary via
    the run's ControlBus and performs cancellation in the asyncio event loop.
    """

    manager_task = asyncio.create_task(worker)
    if not enabled:
        return await manager_task

    from .supervisor.control_bus import ControlBus

    bus = ControlBus(run_dir)

    async def watch_control() -> None:
        while not manager_task.done():
            status = bus.read_status()
            action = str(status.get("requested_action") or "").strip().lower()
            if action in {"stop", "abort"}:
                manager_task.cancel()
                return
            await asyncio.sleep(poll_interval)

    watcher = asyncio.create_task(watch_control())
    try:
        return await manager_task
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass


def _run_command(args: argparse.Namespace) -> int:
    # The agents work in the directory lh-harness was started from, so a task acts
    # on the user's real project by default. Resolve it before touching the disk:
    # every other path below is relative to it.
    if args.workspace:
        workspace = str(Path(args.workspace).expanduser().resolve())
        Path(workspace).mkdir(parents=True, exist_ok=True)
    else:
        workspace = DEFAULT_WORKSPACE_PATH
        if not Path(workspace).is_dir():
            print(
                f"The launch directory no longer exists: {workspace}\n"
                "cd into an existing directory, or pass --workspace.",
                file=sys.stderr,
            )
            return 1

    max_rounds = args.max_rounds
    if max_rounds is None:
        max_rounds = _DEFAULT_MAX_ROUNDS
        print(f"--max-rounds was not set; using the default of {max_rounds} rounds.")
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 1 <= max_rounds <= MAX_ROUNDS:
        print(f"Cannot start run: max_rounds must be an integer from 1 to {MAX_ROUNDS}", file=sys.stderr)
        return 2

    # Validate the supervisor reservation before opening any task reference.
    # Otherwise a malformed ``--supervised --task=@...`` invocation could make
    # this worker read an arbitrary regular file even though adoption would
    # later be rejected by the owner/status checks.
    pre_adopted: tuple[str, Path] | None = None
    try:
        if getattr(args, "supervised", False):
            pre_adopted = _adopt_supervised_run_dir(
                args.runs_root,
                args.run_id,
                task=None,
                agent=args.agent,
                model=args.model,
                role_configs=_public_role_configs_from_args(args),
                workspace=workspace,
                max_rounds=max_rounds,
            )
            task = _read_supervised_task(args.task, run_dir=pre_adopted[1])
        else:
            task = _read_task(args.task)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Cannot start run: {exc}", file=sys.stderr)
        return 2

    # Each run is fully isolated under <runs-root>/<run-id>/ so a new run never
    # mixes with a previous run's tmp/log/workspace data (and the dashboard shows
    # only the current run).
    try:
        if getattr(args, "supervised", False):
            # The supervisor has already reserved this directory and bound its
            # owner PID. Do not call _reserve_run_dir(), which would correctly
            # reject an existing directory for ordinary CLI invocations.
            run_id, run_dir = _adopt_supervised_run_dir(
                args.runs_root,
                args.run_id,
                task=task,
                agent=args.agent,
                model=args.model,
                role_configs=_public_role_configs_from_args(args),
                workspace=workspace,
                max_rounds=max_rounds,
            )
            if pre_adopted is not None and run_id != pre_adopted[0]:
                raise ValueError("supervised run reservation changed during bootstrap")
            _claim_supervised_owner(run_id, run_dir)
        else:
            run_id, run_dir = _reserve_run_dir(args.runs_root, args.run_id)
    except ValueError as exc:
        print(f"Cannot start run: {exc}", file=sys.stderr)
        return 2

    log_dir = str(Path(args.log_dir).expanduser() if args.log_dir else run_dir / "lh_harness")
    if getattr(args, "supervised", False):
        # Supervisor workers always write into their reservation. A hidden flag
        # must not turn into a way to redirect logs/metadata elsewhere.
        try:
            run_root = run_dir.resolve()
            Path(log_dir).expanduser().resolve(strict=False).relative_to(run_root)
        except (OSError, RuntimeError, ValueError):
            print("Cannot start run: supervised log directory escapes the run reservation", file=sys.stderr)
            return 2
    prompt_path = run_dir / "tmp" / "prompts"
    prompt_dir = str(prompt_path)
    harness_dir = (
        str(Path(args.harness_dir).expanduser())
        if args.harness_dir
        else str((run_dir / "harness").resolve())
    )
    try:
        Path(workspace).mkdir(parents=True, exist_ok=True)
        # Logs, prompts, and task scratch space are part of the run boundary.
        # An agent must not be able to redirect any of them through a swapped
        # parent-directory symlink during worker bootstrap.
        _ensure_dir_nofollow(Path(log_dir))
        _ensure_dir_nofollow(prompt_path)
    except OSError as exc:
        print(f"Cannot start run: unsafe run directory layout: {exc}", file=sys.stderr)
        return 2

    # The workspace is the user's own directory, so the run's bookkeeping may sit
    # inside it. Hide those paths from the agents and from the auditor read-only
    # guard, which would otherwise flag the harness's own writes.
    hidden_paths = _outermost_paths(
        PROJECT_CONFIG_PATH.parent,
        args.runs_root,
        run_dir,
        log_dir,
        harness_dir,
        prompt_dir,
    )

    print(f"Run id:    {run_id}")
    print(f"Run dir:   {run_dir.resolve()}")
    print(f"Workspace: {workspace}")
    print(f"Log dir:   {Path(log_dir).resolve()}")

    config = HarnessConfig(
        max_total_episodes=max_rounds,
        manager_budget=EpisodeBudget(max_duration_seconds=args.manager_timeout),
        gui_executor_budget=EpisodeBudget(max_duration_seconds=args.gui_executor_timeout),
        cli_executor_budget=EpisodeBudget(max_duration_seconds=args.cli_executor_timeout),
        auditor_budget=EpisodeBudget(max_duration_seconds=args.auditor_timeout),
        workspace_path=workspace,
        harness_dir=harness_dir,
        log_dir=log_dir,
        prompt_language=args.prompt_language,
    )
    env = _build_env(args.env, tmp_dir=str(run_dir / "tmp"))

    # The dashboard starts before agent creation so startup status is visible.
    dashboard_handle = None
    human_hook = None
    dashboard_supervisor = None
    if getattr(args, "supervised", False):
        from .dashboard import make_human_hook
        from .dashboard.state import DashboardState

        # A Worker has no HTTP server of its own. It still needs the same
        # durable human gate adapter so a separate Supervisor/API process can
        # resolve approvals and queue instructions through the run control bus.
        worker_state = DashboardState(log_dir, task=task, control_enabled=True)
        human_hook = make_human_hook(worker_state)
    if getattr(args, "dashboard", False):
        from .dashboard import make_human_hook

        try:
            from .webapi.server import start_web_server
            from .supervisor.service import RunSupervisor

            # This invocation owns the worker process itself (there is no
            # parent ``Popen`` handle), so attach a restricted supervisor to
            # the current run.  It enables safe stop/abort and status
            # projection while deliberately disallowing create/resume of
            # unrelated workers from the embedded dashboard.
            dashboard_supervisor = RunSupervisor(
                args.runs_root,
                workspace_root=workspace,
                attached_only=True,
            )
            dashboard_supervisor.attach_run(
                run_id=run_id,
                pid=os.getpid(),
                task=task,
                agent=args.agent,
                model=args.model,
                role_configs=_public_role_configs_from_args(args),
                workspace=workspace,
                max_rounds=max_rounds,
                prompt_language=args.prompt_language,
                command=[sys.executable, *sys.argv],
            )

            dashboard_handle = start_web_server(
                log_dir=log_dir,
                runs_root=args.runs_root,
                run_id=run_id,
                task=task,
                control_enabled=bool(task),
                workspace_root=workspace,
                host=args.dashboard_host,
                port=args.dashboard_port,
                supervisor=dashboard_supervisor,
                auth_token=args.dashboard_auth_token,
            )
        except ValueError as exc:
            # attach_run may have written an owner before a bind/auth failure.
            # Do not leave an embedded dashboard advertising a live worker when
            # no server was actually started.
            _finalize_embedded_supervisor(
                dashboard_supervisor,
                run_id,
                report={"status": "failed"},
                returncode=2,
                reason="embedded dashboard refused to start",
            )
            print(f"Dashboard refused to start: {exc}", file=sys.stderr)
            return 2
        except (ImportError, RuntimeError) as exc:
            _finalize_embedded_supervisor(
                dashboard_supervisor,
                run_id,
                report={"status": "failed"},
                returncode=2,
                reason=f"dashboard startup failed: {exc}",
            )
            print(f"Dashboard failed to start: {exc}", file=sys.stderr)
            return 2
        human_hook = make_human_hook(dashboard_handle.state)
        print(f"Dashboard live at {dashboard_handle.url} (log dir: {log_dir})")
        _write_dashboard_endpoint(run_dir / "dashboard.json", dashboard_handle, run_id=run_id, log_dir=log_dir)
        if not args.dashboard_no_open:
            _open_browser_when_ready(dashboard_handle.url)

    agent_cache: dict[tuple[str, str, str | None], object] = {}
    plugin_mcp_cache: dict[str, str | None] = {}

    def resolve_mcp_config(agent_name: str) -> str | None:
        # The agent's own --*-mcp-config wins; otherwise the installed
        # computer-use plugin with the highest priority is loaded for this agent.
        override = getattr(args, _MCP_CONFIG_DESTS[agent_name], None)
        if override:
            return override
        if agent_name not in plugin_mcp_cache:
            from .plugins import PluginError, active_plugin_for_agent

            try:
                active = active_plugin_for_agent(agent_name)
            except PluginError as exc:
                print(f"Warning: could not read the plugin state: {exc}", file=sys.stderr)
                active = None
            if active is None:
                plugin_mcp_cache[agent_name] = None
            else:
                plugin_id, config = active
                origin = config or "loaded natively by the agent"
                print(f"Computer use for {agent_name}: {plugin_id} ({origin})")
                plugin_mcp_cache[agent_name] = config or None
        return plugin_mcp_cache[agent_name]

    def build_role_agent(role: str, *, permission_role: str | None = None):
        # Agent and model resolve independently down the same fallback chain, so
        # mixing backends never sends one backend the other's model id. The
        # permission role is part of the cache key: two Claude roles using the
        # same model must never share a differently privileged adapter.
        name = _resolve_role_option(args, role, "agent")
        model = _resolve_role_model(args, role)
        effective_permission_role = permission_role or role
        key = (effective_permission_role, name, model)
        if key not in agent_cache:
            agent_cache[key] = _build_agent(
                name,
                role=effective_permission_role,
                model=model,
                api_key=args.api_key,
                base_url=args.base_url,
                workspace_path=workspace,
                prompt_dir=prompt_dir,
                mcp_config=resolve_mcp_config(name),
                mcp_add_dirs=args.mcp_add_dir,
                hidden_paths=hidden_paths,
            )
        return agent_cache[key]

    try:
        role_agents = {
            "manager_agent": build_role_agent("manager"),
            "gui_executor_agent": build_role_agent("gui_executor"),
            "cli_executor_agent": build_role_agent("cli_executor"),
            "gui_auditor_agent": build_role_agent("gui_auditor"),
            "cli_auditor_agent": build_role_agent("cli_auditor"),
            # Format repair sees only the previous auditor text and has no tools.
            # It inherits the selected auditor backend/model, but never its audit
            # permissions.
            "auditor_format_repair_agent": build_role_agent(
                "auditor",
                permission_role="auditor_format_repair",
            ),
            "final_response_agent": build_role_agent("final_response"),
        }
    except BaseException as exc:
        _write_bootstrap_failure(log_dir, task, exc, max_rounds=max_rounds)
        print(f"Worker failed during agent setup: {exc}", file=sys.stderr)
        _finalize_embedded_supervisor(
            dashboard_supervisor,
            run_id,
            report={"status": "failed"},
            returncode=1,
            reason="worker bootstrap failure",
        )
        if dashboard_handle is not None:
            dashboard_handle.shutdown()
        return 1

    from .manager import run

    report: dict[str, object] | None = None
    try:
        report = asyncio.run(
            _run_with_attached_control(
                run(
                    task=task,
                    env=env,
                    config=config,
                    human_hook=human_hook,
                    progress=_print_progress,
                    **role_agents,
                ),
                run_dir=run_dir,
                enabled=dashboard_supervisor is not None,
            )
        )
    except BaseException as exc:
        _finalize_embedded_supervisor(
            dashboard_supervisor,
            run_id,
            report={"status": "failed"},
            returncode=1,
            reason=f"worker failed: {exc}",
        )
        raise
    else:
        # A hosted dashboard keeps this same PID alive after Manager returns;
        # finalize before entering that serving loop so the right panel and
        # later API clients see a terminal run immediately.
        _finalize_embedded_supervisor(
            dashboard_supervisor,
            run_id,
            report=report,
            returncode=0,
        )
    finally:
        # The summary is printed before the dashboard blocks, so the outcome is
        # visible in the console even when the operator leaves the UI running.
        if report is not None:
            _print_run_summary(report, log_dir=log_dir, workspace=workspace)
        if dashboard_handle is not None:
            if _should_keep_embedded_dashboard(
                run_dir,
                explicitly_requested=bool(args.keep_dashboard),
                report=report,
            ):
                print(f"\nDashboard still live at {dashboard_handle.url}; press Ctrl+C to exit.")
                try:
                    dashboard_handle.serve_forever_blocking()
                except KeyboardInterrupt:
                    dashboard_handle.shutdown()
            else:
                dashboard_handle.shutdown()

    final_report = report or {"status": "failed", "completion_satisfied": False}
    return 0 if final_report.get("completion_satisfied") else 1


def _outermost_paths(*paths: str | Path) -> tuple[str, ...]:
    """Resolve paths and drop any that sit inside another, keeping the parents."""
    resolved = sorted({Path(item).expanduser().resolve() for item in paths}, key=lambda p: len(p.parts))
    kept: list[Path] = []
    for path in resolved:
        if not any(path.is_relative_to(parent) for parent in kept):
            kept.append(path)
    return tuple(str(path) for path in kept)


def _open_dashboard(url: str) -> None:
    import webbrowser

    try:
        opened = webbrowser.open_new_tab(url)
    except Exception:
        opened = False
    if not opened:
        print(f"Could not open a browser automatically; open {url} manually.")


def _print_progress(event: str, payload: dict) -> None:
    """Print one console line per role transition so a long run stays legible."""
    round_index = payload.get("round")
    if event == "round_start":
        print(f"\n── Round {round_index}/{payload.get('round_budget')} ──", flush=True)
    elif event == "role_start":
        # The reply is written when the run reaches an ending, so it gets its own
        # heading instead of appearing to be part of that round's work.
        if payload.get("role") == "final_response":
            print("\n── Writing reply ──", flush=True)
        print(f"  [{payload.get('role')}] running...", flush=True)
    elif event == "role_done":
        parts = [str(payload.get("status"))]
        duration_ms = payload.get("duration_ms")
        if isinstance(duration_ms, int):
            parts.append(f"{duration_ms / 1000:.1f}s")
        if payload.get("next_step"):
            parts.append(f"next={payload['next_step']}")
        if payload.get("audit_status"):
            parts.append(
                f"audit={payload['audit_status']}/"
                f"{payload.get('integrity_status')}/{payload.get('contract_audit_status')}"
            )
        print(f"  [{payload.get('role')}] {' · '.join(parts)}", flush=True)


def _print_run_summary(report: dict, *, log_dir: str, workspace: str) -> None:
    print("\n" + "=" * 72)
    print(f"Result:    {report.get('status')}")
    print(f"Rounds:    {report.get('rounds_run')}/{report.get('max_rounds')}")
    elapsed = report.get("elapsed_seconds")
    if isinstance(elapsed, (int, float)):
        print(f"Elapsed:   {elapsed / 60:.1f} min")
    if report.get("abort_reason"):
        print(f"Stopped:   {report['abort_reason']}")
    print(f"Workspace: {workspace}")
    print(f"Report:    {Path(log_dir).resolve() / 'report.json'}")

    # The reply answers the task in prose, so it leads. The protocol artifacts
    # below it stay for anyone auditing how that answer was reached.
    response = str(report.get("final_response") or "").strip()
    if response:
        print("\n" + "-" * 72)
        print(_indent(response))
        print("-" * 72)
    for label, key in (("Task state", "current_task_state"), ("Final audit", "latest_auditor_report")):
        text = str(report.get(key) or "").strip()
        if text:
            print(f"\n{label}:\n{_indent(text)}")
    print("=" * 72)


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _resolve_role_option(args: argparse.Namespace, role: str, suffix: str):
    """Walk a role's fallback chain up to the global `--agent` / `--model`."""
    while role:
        value = getattr(args, f"{role}_{suffix}", None)
        if value:
            return value
        role = _ROLE_PARENTS[role]
    return getattr(args, suffix)


def _resolve_role_model(args: argparse.Namespace, role: str) -> str | None:
    """Resolve a model without crossing an explicit backend switch.

    Agent and model overrides have parallel fallback chains, but a Claude role
    must never inherit the global Codex model merely because only its agent was
    overridden.  A model set at or below the nearest explicit agent boundary
    remains valid; otherwise the selected provider gets its own default.
    """

    current: str | None = role
    while current:
        value = getattr(args, f"{current}_model", None)
        if value:
            return value
        if getattr(args, f"{current}_agent", None):
            return None
        current = _ROLE_PARENTS[current]
    return getattr(args, "model", None)


def _public_role_configs_from_args(
    args: argparse.Namespace,
) -> dict[str, dict[str, str | None]] | None:
    """Return effective Manager/Executor/Auditor bindings when overridden."""

    public_roles = ("manager", "executor", "auditor")
    if not any(
        getattr(args, f"{role}_{field}", None)
        for role in public_roles
        for field in ("agent", "model")
    ):
        return None
    defaults = {
        "codex": DEFAULT_CODEX_MODEL,
        "claude_code": DEFAULT_CLAUDE_MODEL,
    }
    result: dict[str, dict[str, str | None]] = {}
    for role in public_roles:
        role_agent = _resolve_role_option(args, role, "agent")
        result[role] = {
            "agent": role_agent,
            "model": _resolve_role_model(args, role) or defaults[role_agent],
        }
    return result


def _write_bootstrap_failure(log_dir: str, task: str, exc: BaseException, *, max_rounds: int) -> None:
    """Persist a terminal report when agent construction fails before Manager."""

    root = Path(log_dir)
    role_dir = root / "role_orchestration"
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    report = {
        "schema_version": 2,
        "status": "failed",
        "task": task,
        "completion_satisfied": False,
        "completion_authority": "manager_with_role_auditors",
        "rounds_run": 0,
        "max_rounds": max_rounds,
        "abort_reason": "worker_bootstrap_failure",
        "error": str(exc),
        "exception_type": type(exc).__name__,
        "traceback_tail": trace[-12000:],
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    for target in (root / "report.json", role_dir / "report.json"):
        try:
            _atomic_bytes_write(target, encoded.encode("utf-8"))
        except OSError:
            pass
    events = role_dir / "events.jsonl"
    try:
        _append_jsonl_nofollow(events, {
            "schema_version": 1,
            "event": "role_harness_failed",
            "status": "failed",
            "ts": time.time(),
            "reason": str(exc),
            "exception_type": type(exc).__name__,
            "traceback_tail": trace[-4000:],
        })
    except OSError:
        pass


def _read_supervised_task(raw: str, *, run_dir: Path) -> str:
    """Read only the supervisor's reserved ``tmp/task.md`` contract.

    ``_read_task`` deliberately supports arbitrary ``@path`` values for the
    public CLI.  A supervised worker is different: its command is generated
    by the supervisor and must never become a confused-deputy file reader if
    someone invokes the hidden flag manually or tampers with its argv.
    """

    if not raw.startswith("@"):
        raise ValueError("supervised workers require a @task-file reference")
    expected = (run_dir / "tmp" / "task.md").absolute()
    supplied = Path(raw[1:]).expanduser()
    supplied = Path(os.path.abspath(os.fspath(supplied)))
    if supplied != expected:
        raise ValueError("supervised task file must be the reserved run/tmp/task.md")
    return _read_task(raw)


def _read_task(raw: str) -> str:
    if raw.startswith("@"):
        path = Path(raw[1:]).expanduser()
        fd: int | None = None
        try:
            # Supervisor-created task files live below a worker-writable run
            # tree.  Read through an anchored descriptor so a replacement of
            # ``tmp`` or ``task.md`` between launch and bootstrap cannot make
            # the worker consume a different file (or an external secret).
            fd = _open_nofollow(path)
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("task file is not a private regular file")
            if metadata.st_size > _MAX_TASK_FILE_BYTES:
                raise ValueError("task file is too large")
            raw_bytes = os.read(fd, _MAX_TASK_FILE_BYTES + 1)
            if len(raw_bytes) > _MAX_TASK_FILE_BYTES:
                raise ValueError("task file is too large")
            return raw_bytes.decode("utf-8").strip()
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
    return raw


def _build_env(spec: str, *, tmp_dir: str | None = None):
    if spec == "local":
        from .environment.local import LocalEnvironment

        return LocalEnvironment(tmp_dir=tmp_dir)
    raise ValueError(f"Unknown env: {spec}")


def _build_agent(
    name: str,
    *,
    role: str,
    model: str | None,
    api_key: str | None,
    base_url: str | None,
    workspace_path: str,
    prompt_dir: str,
    mcp_config: str | None = None,
    mcp_add_dirs: list[str] | None = None,
    hidden_paths: tuple[str, ...] = (),
):
    if name == "codex":
        from .adapters.codex import CodexAdapter

        kwargs = dict(
            api_key=api_key,
            base_url=base_url,
            workspace_path=workspace_path,
            prompt_dir=prompt_dir,
            mcp_config=mcp_config,
            add_dirs=mcp_add_dirs,
            hidden_paths=hidden_paths,
        )
        if model is not None:
            kwargs["model"] = model
        return CodexAdapter(**kwargs)
    if name == "claude_code":
        from .adapters.claude_code import ClaudeCodeAdapter

        kwargs = dict(
            api_key=api_key,
            base_url=base_url,
            workspace_path=workspace_path,
            prompt_dir=prompt_dir,
            mcp_config=mcp_config,
            add_dirs=mcp_add_dirs,
            role=role,
            hidden_paths=hidden_paths,
        )
        if model is not None:
            kwargs["model"] = model
        return ClaudeCodeAdapter(**kwargs)
    raise ValueError(f"Unknown agent: {name}")


if __name__ == "__main__":
    raise SystemExit(main())
