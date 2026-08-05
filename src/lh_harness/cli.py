from __future__ import annotations

import argparse
import asyncio
import json
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from . import HOMEPAGE, ISSUES_URL, __version__
from .config import (
    PROJECT_CONFIG_PATH,
    ProjectConfigError,
    create_project_config,
    load_run_defaults,
)
from .types import DEFAULT_CLAUDE_MODEL, DEFAULT_CODEX_MODEL, EpisodeBudget, HarnessConfig

if TYPE_CHECKING:
    from .utils import UpdateCheckResult

_EPILOG = f"Homepage: {HOMEPAGE}\nFound a bug? Please open an issue: {ISSUES_URL}"

# Runs are project-scoped.
_DEFAULT_RUNS_ROOT = "./.lh-harness/runs"
_DEFAULT_MAX_ROUNDS = 30

# Agent backends as (choice, CLI binary, default model).
_AGENTS = (
    ("claude_code", "claude", DEFAULT_CLAUDE_MODEL),
    ("codex", "codex", DEFAULT_CODEX_MODEL),
)
_AGENT_CHOICES = tuple(name for name, _, _ in _AGENTS)

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
)
_ROLE_PARENTS = {role: parent for role, parent, _ in _ROLE_OPTIONS}
_ROLE_SCOPES = {role: scope for role, _, scope in _ROLE_OPTIONS}

# Per-role episode budgets as (dest prefix, timeout seconds). The
# executors get the long task timeout; the scheduler and auditors get the short one.
_BUDGET_OPTIONS = (
    ("manager", 600),
    ("gui_executor", 1800),
    ("cli_executor", 1800),
    ("auditor", 600),
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
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


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
        help="Override the workspace path. Defaults to <runs-root>/<run-id>/workspace.",
    )
    run_parser.add_argument(
        "--harness-dir",
        default=run_default("harness_dir"),
        help="Override the harness state directory. Defaults to <workspace>/.harness.",
    )
    run_parser.add_argument(
        "--log-dir",
        default=run_default("log_dir"),
        help="Override the log directory. Defaults to <runs-root>/<run-id>/logs.",
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
    run_parser.add_argument(
        "--mcp-config",
        default=run_default("mcp_config"),
        help="Optional MCP config path (Claude `.mcp.json` shape; translated for Codex). No MCP server is enabled by default.",
    )
    run_parser.add_argument(
        "--mcp-add-dir",
        action="append",
        default=None,
        help="Extra directory to expose to the agent. May be repeated.",
    )
    run_parser.add_argument(
        "--max-rounds",
        type=_positive_int,
        default=run_default("max_rounds"),
        help=f"Maximum number of manage-execute-audit rounds. If omitted, uses {_DEFAULT_MAX_ROUNDS}.",
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
        type=int,
        default=run_default("dashboard_port", 0),
        help="Dashboard port; 0 lets the OS pick a free one.",
    )

    dash_parser = add_command("dashboard", "Serve the dashboard to browse runs / a run log directory")
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
    dash_parser.add_argument("--port", type=int, default=0, help="Dashboard port; 0 lets the OS pick a free one.")

    doctor_parser = add_command("doctor", "Check the local environment and optional Codex GUI support")
    codex_gui_actions = doctor_parser.add_mutually_exclusive_group()
    codex_gui_actions.add_argument(
        "--install-codex-gui",
        action="store_true",
        help="Install and enable the official Codex Computer Use plugin after explicit opt-in.",
    )
    codex_gui_actions.add_argument(
        "--uninstall-codex-gui",
        action="store_true",
        help="Remove the official Codex Computer Use plugin after explicit opt-in.",
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
    if args.command == "doctor":
        return _doctor_command(args)
    if args.command == "init":
        return _init_command(args)
    if args.command == "check-update":
        return _check_update_command()

    parser.print_help()
    return 2


def _doctor_command(args: argparse.Namespace) -> int:
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
        path = shutil.which(binary)
        if not path:
            _doctor_line("WARN", name, f"`{binary}` was not found on PATH")
            warnings += 1
            continue
        found_agents[name] = path
        version = _agent_version(path)
        _doctor_line("OK", name, f"{version + ' ' if version else ''}({path})")

    if not found_agents:
        _doctor_line("FAIL", "Agent runtime", "install Claude Code or Codex CLI and add it to PATH")
        failures += 1

    codex_path = found_agents.get("codex")
    if codex_path:
        from .codex_plugins import (
            COMPUTER_USE_PLUGIN_ID,
            CodexPluginError,
            get_codex_plugin_state,
            install_computer_use_plugin,
            uninstall_computer_use_plugin,
        )

        try:
            if args.install_codex_gui:
                state = install_computer_use_plugin(
                    codex_binary=codex_path,
                    on_status=lambda status, message: _doctor_line(status.upper(), "Codex GUI", message),
                )
            elif args.uninstall_codex_gui:
                state = uninstall_computer_use_plugin(
                    codex_binary=codex_path,
                    on_status=lambda status, message: _doctor_line(status.upper(), "Codex GUI", message),
                )
            else:
                state = get_codex_plugin_state(COMPUTER_USE_PLUGIN_ID, codex_binary=codex_path)
        except CodexPluginError as exc:
            changing_plugin = args.install_codex_gui or args.uninstall_codex_gui
            level = "FAIL" if changing_plugin else "WARN"
            _doctor_line(level, "Codex GUI", str(exc))
            if changing_plugin:
                failures += 1
            else:
                warnings += 1
        else:
            if args.uninstall_codex_gui:
                _doctor_line("OK", "Codex GUI", f"{state.plugin_id} is not installed")
            elif state.ready:
                version = f" {state.version}" if state.version else ""
                _doctor_line("OK", "Codex GUI", f"{state.plugin_id}{version} is installed and enabled")
            elif state.available:
                detail = "installed but disabled" if state.installed else "available but not installed"
                _doctor_line(
                    "WARN",
                    "Codex GUI",
                    f"{state.plugin_id} is {detail}; run `lh-harness doctor --install-codex-gui`",
                )
                warnings += 1
            else:
                _doctor_line("WARN", "Codex GUI", f"{state.plugin_id} is unavailable; update Codex CLI")
                warnings += 1
    elif args.install_codex_gui or args.uninstall_codex_gui:
        action = "install" if args.install_codex_gui else "uninstall"
        _doctor_line("FAIL", "Codex GUI", f"Codex CLI is required to {action} Computer Use")
        failures += 1
    else:
        _doctor_line("SKIP", "Codex GUI", "Codex CLI is not installed")

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


def _agent_version(path: str) -> str:
    """Best-effort `<agent> --version`; an unresponsive CLI must not fail the command."""
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    lines = (result.stdout or result.stderr).strip().splitlines()
    if not lines:
        return ""
    # Each CLI decorates the number differently ("2.1.220 (Claude Code)",
    # "codex-cli 0.146.0"), so keep just the version itself.
    match = re.search(r"\d+(?:\.\d+)+\S*", lines[0])
    return match.group(0) if match else lines[0].strip()


def _dashboard_command(args: argparse.Namespace) -> int:
    from .dashboard import start_dashboard

    if args.log_dir:
        handle = start_dashboard(args.log_dir, port=args.port)
        print(f"Dashboard serving {args.log_dir} at {handle.url}")
    else:
        handle = start_dashboard(runs_root=args.runs_root, port=args.port)
        print(f"Dashboard browsing runs under {args.runs_root} at {handle.url}")
        print("Use the run selector in the top bar to switch between runs.")
    print("Press Ctrl+C to stop.")
    try:
        handle.serve_forever_blocking()
    except KeyboardInterrupt:
        handle.shutdown()
    return 0


def _run_command(args: argparse.Namespace) -> int:
    task = _read_task(args.task)
    max_rounds = args.max_rounds
    if max_rounds is None:
        max_rounds = _DEFAULT_MAX_ROUNDS
        print(f"未指定 --max-rounds，使用默认值：{max_rounds} 轮。")

    # Each run is fully isolated under <runs-root>/<run-id>/ so a new run never
    # mixes with a previous run's tmp/log/workspace data (and the dashboard shows
    # only the current run).
    run_id = args.run_id or f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:8]}"
    run_dir = Path(args.runs_root).expanduser() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    workspace = str(Path(args.workspace).expanduser() if args.workspace else run_dir / "workspace")
    log_dir = str(Path(args.log_dir).expanduser() if args.log_dir else run_dir / "logs")
    prompt_dir = str((run_dir / "tmp" / "prompts").resolve())
    harness_dir = args.harness_dir or f"{workspace}/.harness"
    Path(workspace).mkdir(parents=True, exist_ok=True)
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    print(f"Run id:    {run_id}")
    print(f"Run dir:   {run_dir.resolve()}")
    print(f"Workspace: {Path(workspace).resolve()}")
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
    if getattr(args, "dashboard", False):
        from .dashboard import make_human_hook, start_dashboard

        dashboard_handle = start_dashboard(log_dir, port=args.dashboard_port, task=task)
        human_hook = make_human_hook(dashboard_handle.state)
        print(f"Dashboard live at {dashboard_handle.url} (log dir: {log_dir})")

    agent_cache: dict[tuple[str, str | None], object] = {}

    def build_role_agent(role: str = ""):
        # Agent and model resolve independently down the same fallback chain, so
        # mixing backends never sends one backend the other's model id.
        name = _resolve_role_option(args, role, "agent")
        model = _resolve_role_option(args, role, "model")
        key = (name, model)
        if key not in agent_cache:
            agent_cache[key] = _build_agent(
                name,
                model=model,
                api_key=args.api_key,
                base_url=args.base_url,
                workspace_path=workspace,
                prompt_dir=prompt_dir,
                mcp_config=args.mcp_config,
                mcp_add_dirs=args.mcp_add_dir,
            )
        return agent_cache[key]

    role_agents = {
        "manager_agent": build_role_agent("manager"),
        "gui_executor_agent": build_role_agent("gui_executor"),
        "cli_executor_agent": build_role_agent("cli_executor"),
        "gui_auditor_agent": build_role_agent("gui_auditor"),
        "cli_auditor_agent": build_role_agent("cli_auditor"),
    }

    from .manager import run

    try:
        report = asyncio.run(
            run(
                task=task,
                env=env,
                config=config,
                human_hook=human_hook,
                **role_agents,
            )
        )
    finally:
        if dashboard_handle is not None:
            print(f"Run finished. Dashboard still live at {dashboard_handle.url} — press Ctrl+C to exit.")
            try:
                dashboard_handle.serve_forever_blocking()
            except KeyboardInterrupt:
                dashboard_handle.shutdown()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("completion_satisfied") else 1


def _resolve_role_option(args: argparse.Namespace, role: str, suffix: str):
    """Walk a role's fallback chain up to the global `--agent` / `--model`."""
    while role:
        value = getattr(args, f"{role}_{suffix}", None)
        if value:
            return value
        role = _ROLE_PARENTS[role]
    return getattr(args, suffix)


def _read_task(raw: str) -> str:
    if raw.startswith("@"):
        return Path(raw[1:]).read_text(encoding="utf-8").strip()
    return raw


def _build_env(spec: str, *, tmp_dir: str | None = None):
    if spec == "local":
        from .environment.local import LocalEnvironment

        return LocalEnvironment(tmp_dir=tmp_dir)
    raise ValueError(f"Unknown env: {spec}")


def _build_agent(
    name: str,
    *,
    model: str | None,
    api_key: str | None,
    base_url: str | None,
    workspace_path: str,
    prompt_dir: str,
    mcp_config: str | None = None,
    mcp_add_dirs: list[str] | None = None,
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
        )
        if model is not None:
            kwargs["model"] = model
        return ClaudeCodeAdapter(**kwargs)
    raise ValueError(f"Unknown agent: {name}")


if __name__ == "__main__":
    raise SystemExit(main())
