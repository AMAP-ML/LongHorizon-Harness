from __future__ import annotations

import argparse
import asyncio
import json
import platform
import re
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
from .utils.agent_cli import probe_agent_cli

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


def _port(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("must be between 0 and 65535")
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
        type=_port,
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
    dash_parser.add_argument("--port", type=_port, default=0, help="Dashboard port; 0 lets the OS pick a free one.")

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
    """Report the Node/npm toolchain the npm-distributed plugins need."""
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
        _doctor_line("WARN", "Node.js", "`node --version` was unreadable; install Node.js 20 or later")
        return warnings + 1
    major_match = re.match(r"(\d+)", node)
    major = int(major_match.group(1)) if major_match else 0
    if major >= _MIN_NODE_MAJOR:
        _doctor_line("OK", "Node.js", node)
    else:
        _doctor_line("WARN", "Node.js", f"{node} is older than {_MIN_NODE_MAJOR}; plugins may fail")
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
        print(f"--max-rounds was not set; using the default of {max_rounds} rounds.")

    # Each run is fully isolated under <runs-root>/<run-id>/ so a new run never
    # mixes with a previous run's tmp/log/workspace data (and the dashboard shows
    # only the current run).
    run_id = args.run_id or f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:8]}"
    run_dir = Path(args.runs_root).expanduser() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    workspace = str(Path(args.workspace).expanduser() if args.workspace else run_dir / "workspace")
    log_dir = str(Path(args.log_dir).expanduser() if args.log_dir else run_dir / "logs")
    prompt_dir = str((run_dir / "tmp" / "prompts").resolve())
    harness_dir = (
        str(Path(args.harness_dir).expanduser()) if args.harness_dir else f"{workspace}/.harness"
    )
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
        model = _resolve_role_option(args, role, "model")
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
            print(f"Run finished. Dashboard still live at {dashboard_handle.url}; press Ctrl+C to exit.")
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
    role: str,
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
            role=role,
        )
        if model is not None:
            kwargs["model"] = model
        return ClaudeCodeAdapter(**kwargs)
    raise ValueError(f"Unknown agent: {name}")


if __name__ == "__main__":
    raise SystemExit(main())
