from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

from .types import EpisodeBudget, HarnessConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lh-harness")
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Run a long-horizon task through role-managed LongHorizon-Harness")
    run_parser.add_argument("--task", required=True, help="Task text or @path")
    run_parser.add_argument("--agent", default="claude_code", choices=["openclaw", "codex", "claude_code"])
    run_parser.add_argument(
        "--manager-agent",
        choices=["openclaw", "codex", "claude_code"],
        help="Agent implementation for the scheduler role; defaults to --agent.",
    )
    run_parser.add_argument(
        "--gui-executor-agent",
        choices=["openclaw", "codex", "claude_code"],
        help="Agent implementation for GUI/visual subtasks; defaults to --agent.",
    )
    run_parser.add_argument(
        "--cli-executor-agent",
        choices=["openclaw", "codex", "claude_code"],
        help="Agent implementation for CLI/non-GUI subtasks; defaults to --agent.",
    )
    run_parser.add_argument(
        "--auditor-agent",
        choices=["openclaw", "codex", "claude_code"],
        help="Agent implementation for both auditor roles; defaults to --agent.",
    )
    run_parser.add_argument(
        "--gui-auditor-agent",
        choices=["openclaw", "codex", "claude_code"],
        help="Agent implementation for GUI audit; defaults to --auditor-agent or --agent.",
    )
    run_parser.add_argument(
        "--cli-auditor-agent",
        choices=["openclaw", "codex", "claude_code"],
        help="Agent implementation for CLI audit; defaults to --auditor-agent or --agent.",
    )
    run_parser.add_argument("--env", default="local", help="local | ssh://user@host:port | docker://container")
    run_parser.add_argument(
        "--run-root",
        default="./runs",
        help="Base directory holding one isolated subfolder per run.",
    )
    run_parser.add_argument(
        "--run-id",
        default=None,
        help="Unique id for this run. Defaults to a timestamp + short uuid. All run data goes under <run-root>/<run-id>/.",
    )
    run_parser.add_argument(
        "--workspace",
        default=None,
        help="Override the workspace path. Defaults to <run-root>/<run-id>/workspace.",
    )
    run_parser.add_argument("--harness-dir")
    run_parser.add_argument(
        "--log-dir",
        default=None,
        help="Override the log directory. Defaults to <run-root>/<run-id>/logs.",
    )
    run_parser.add_argument("--model", default="claude-opus-4-8")
    run_parser.add_argument("--auditor-model", default="claude-opus-4-8")
    run_parser.add_argument("--api-key")
    run_parser.add_argument("--base-url")
    run_parser.add_argument(
        "--mcp-config",
        default=None,
        help="Optional Claude Code MCP config path. No MCP server is enabled by default.",
    )
    run_parser.add_argument(
        "--mcp-add-dir",
        action="append",
        default=[],
        help="Directory to expose to Claude Code. May be repeated.",
    )
    run_parser.add_argument("--max-rounds", type=int, default=30)
    run_parser.add_argument("--manager-max-turns", type=int, default=20)
    run_parser.add_argument("--manager-timeout", type=int, default=300)
    run_parser.add_argument("--gui-executor-max-turns", type=int, default=20)
    run_parser.add_argument("--gui-executor-timeout", type=int, default=1800)
    run_parser.add_argument("--cli-executor-max-turns", type=int, default=20)
    run_parser.add_argument("--cli-executor-timeout", type=int, default=1800)
    run_parser.add_argument("--auditor-max-turns", type=int, default=20)
    run_parser.add_argument("--auditor-timeout", type=int, default=300)
    run_parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Launch the web dashboard in the background for live monitoring and human approval.",
    )
    run_parser.add_argument(
        "--dashboard-port",
        type=int,
        default=0,
        help="Dashboard port. 0 (default) lets the OS pick a free port.",
    )

    dash_parser = sub.add_parser("dashboard", help="Serve the dashboard to browse runs / a run log directory")
    dash_parser.add_argument(
        "--runs-root",
        default="./runs",
        help="Base directory holding runs; the UI lists all runs and lets you switch between them.",
    )
    dash_parser.add_argument(
        "--log-dir",
        default=None,
        help="Pin one run's log directory instead of browsing --runs-root.",
    )
    dash_parser.add_argument("--port", type=int, default=0, help="0 (default) lets the OS pick a free port.")

    args = parser.parse_args(argv)
    if args.command == "run":
        return _run_command(args)
    if args.command == "dashboard":
        return _dashboard_command(args)

    parser.print_help()
    return 2


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

    # Each run is fully isolated under <run-root>/<run-id>/ so a new run never
    # mixes with a previous run's tmp/log/workspace data (and the dashboard shows
    # only the current run).
    run_id = args.run_id or f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:8]}"
    run_dir = Path(args.run_root).expanduser() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    workspace = (args.workspace or str(run_dir / "workspace")).rstrip("/")
    log_dir = args.log_dir or str(run_dir / "logs")
    harness_dir = args.harness_dir or f"{workspace}/.harness"
    Path(workspace).mkdir(parents=True, exist_ok=True)
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    print(f"Run id: {run_id}")
    print(f"Run dir: {run_dir}")
    print(f"Workspace: {workspace}")
    print(f"Log dir: {log_dir}")

    config = HarnessConfig(
        max_total_episodes=args.max_rounds,
        episode_budget=EpisodeBudget(args.cli_executor_max_turns, args.cli_executor_timeout),
        auditor_budget=EpisodeBudget(args.auditor_max_turns, args.auditor_timeout),
        manager_budget=EpisodeBudget(args.manager_max_turns, args.manager_timeout),
        gui_executor_budget=EpisodeBudget(args.gui_executor_max_turns, args.gui_executor_timeout),
        cli_executor_budget=EpisodeBudget(args.cli_executor_max_turns, args.cli_executor_timeout),
        role_auditor_budget=EpisodeBudget(args.auditor_max_turns, args.auditor_timeout),
        workspace_path=workspace,
        harness_dir=harness_dir,
        log_dir=log_dir,
        auditor_model=args.auditor_model,
        agent_model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
    )
    env = _build_env(args.env, tmp_dir=str(run_dir / "tmp"))

    # Each role can run on a different adapter. Defaults keep the CLI simple:
    # one task adapter plus one auditor adapter is enough for Codex/Claude Code.
    agent_cache: dict[tuple[str, str], object] = {}

    def build_role_agent(name: str, *, auditor: bool = False):
        model = args.auditor_model if auditor else args.model
        key = (name, model)
        if key not in agent_cache:
            agent_cache[key] = _build_agent(
                name,
                model=model,
                api_key=args.api_key,
                base_url=args.base_url,
                workspace_path=workspace,
                mcp_config=args.mcp_config,
                mcp_add_dirs=args.mcp_add_dir,
            )
        return agent_cache[key]

    auditor_default = args.auditor_agent or args.agent
    default_agent = build_role_agent(args.agent)
    manager_agent = build_role_agent(args.manager_agent or args.agent)
    gui_executor_agent = build_role_agent(args.gui_executor_agent or args.agent)
    cli_executor_agent = build_role_agent(args.cli_executor_agent or args.agent)
    gui_auditor_agent = build_role_agent(args.gui_auditor_agent or auditor_default, auditor=True)
    cli_auditor_agent = build_role_agent(args.cli_auditor_agent or auditor_default, auditor=True)

    # Optional dashboard: background web server + human-approval round hook.
    # All dashboard code lives in lh_harness.dashboard; this is the only wiring.
    dashboard_handle = None
    human_hook = None
    if getattr(args, "dashboard", False):
        from .dashboard import make_human_hook, start_dashboard

        dashboard_handle = start_dashboard(log_dir, port=args.dashboard_port, task=task)
        # One human-in-the-loop hook covers both per-round approval/injection and
        # the end-of-run "continue?" gate (so the run never ends silently).
        human_hook = make_human_hook(dashboard_handle.state)
        print(f"Dashboard live at {dashboard_handle.url} (log dir: {log_dir})")

    from .manager import run

    try:
        report = asyncio.run(
            run(
                task=task,
                env=env,
                agent=default_agent,
                manager_agent=manager_agent,
                gui_executor_agent=gui_executor_agent,
                cli_executor_agent=cli_executor_agent,
                gui_auditor_agent=gui_auditor_agent,
                cli_auditor_agent=cli_auditor_agent,
                config=config,
                human_hook=human_hook,
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


def _read_task(raw: str) -> str:
    if raw.startswith("@"):
        return Path(raw[1:]).read_text(encoding="utf-8").strip()
    return raw


def _build_env(spec: str, *, tmp_dir: str | None = None):
    if spec == "local":
        from .environment.local import LocalEnvironment

        return LocalEnvironment(tmp_dir=tmp_dir)
    if spec.startswith("ssh://"):
        from .environment.ssh import SSHEnvironment

        return SSHEnvironment.from_url(spec)
    if spec.startswith("docker://"):
        from .environment.docker import DockerEnvironment

        return DockerEnvironment.from_url(spec)
    raise ValueError(f"Unknown env: {spec}")


def _build_agent(
    name: str,
    *,
    model: str,
    api_key: str | None,
    base_url: str | None,
    workspace_path: str,
    mcp_config: str | None = None,
    mcp_add_dirs: list[str] | None = None,
):
    if name == "openclaw":
        from .adapters.openclaw import OpenClawAdapter

        return OpenClawAdapter(model=model, api_key=api_key, base_url=base_url, workspace_path=workspace_path)
    if name == "codex":
        from .adapters.codex import CodexAdapter

        return CodexAdapter(model=model, api_key=api_key, base_url=base_url, workspace_path=workspace_path)
    if name == "claude_code":
        from .adapters.claude_code import ClaudeCodeAdapter

        return ClaudeCodeAdapter(
            model=model,
            api_key=api_key,
            base_url=base_url,
            workspace_path=workspace_path,
            mcp_config=mcp_config,
            add_dirs=mcp_add_dirs,
        )
    raise ValueError(f"Unknown agent: {name}")


if __name__ == "__main__":
    raise SystemExit(main())
