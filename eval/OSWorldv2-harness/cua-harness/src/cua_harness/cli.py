from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .types import EpisodeBudget, HarnessConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cua-harness")
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Run a long-horizon task through role-orchestrated CUA-Harness")
    run_parser.add_argument("--task", required=True, help="Task text or @path")
    run_parser.add_argument("--agent", default="claude_code", choices=["openclaw", "codex", "claude_code"])
    run_parser.add_argument(
        "--orchestrator-agent",
        choices=["openclaw", "codex", "claude_code"],
        help="Agent implementation for the scheduler role; defaults to --agent.",
    )
    run_parser.add_argument(
        "--gui-task-agent",
        choices=["openclaw", "codex", "claude_code"],
        help="Agent implementation for GUI/visual subtasks; defaults to --agent.",
    )
    run_parser.add_argument(
        "--cli-task-agent",
        choices=["openclaw", "codex", "claude_code"],
        help="Agent implementation for CLI/non-GUI subtasks; defaults to --agent.",
    )
    run_parser.add_argument(
        "--verifier-agent",
        choices=["openclaw", "codex", "claude_code"],
        help="Agent implementation for both verifier roles; defaults to --agent.",
    )
    run_parser.add_argument(
        "--gui-verifier-agent",
        choices=["openclaw", "codex", "claude_code"],
        help="Agent implementation for GUI verification; defaults to --verifier-agent or --agent.",
    )
    run_parser.add_argument(
        "--cli-verifier-agent",
        choices=["openclaw", "codex", "claude_code"],
        help="Agent implementation for CLI verification; defaults to --verifier-agent or --agent.",
    )
    run_parser.add_argument("--env", default="local", help="local | ssh://user@host:port | docker://container")
    run_parser.add_argument("--workspace", default="/tmp_workspace")
    run_parser.add_argument("--harness-dir")
    run_parser.add_argument("--log-dir", default="./harness_logs")
    run_parser.add_argument("--model", default="gpt-4o")
    run_parser.add_argument("--verifier-model", default="gpt-4o-mini")
    run_parser.add_argument("--api-key")
    run_parser.add_argument("--base-url")
    run_parser.add_argument("--max-rounds", type=int, default=4)
    run_parser.add_argument("--orchestrator-max-turns", type=int, default=20)
    run_parser.add_argument("--orchestrator-timeout", type=int, default=300)
    run_parser.add_argument("--gui-task-max-turns", type=int, default=20)
    run_parser.add_argument("--gui-task-timeout", type=int, default=1800)
    run_parser.add_argument("--cli-task-max-turns", type=int, default=20)
    run_parser.add_argument("--cli-task-timeout", type=int, default=1800)
    run_parser.add_argument("--verifier-max-turns", type=int, default=20)
    run_parser.add_argument("--verifier-timeout", type=int, default=300)

    args = parser.parse_args(argv)
    if args.command == "run":
        return _run_command(args)

    parser.print_help()
    return 2


def _run_command(args: argparse.Namespace) -> int:
    task = _read_task(args.task)
    workspace = args.workspace.rstrip("/")
    harness_dir = args.harness_dir or f"{workspace}/.harness"
    config = HarnessConfig(
        max_total_episodes=args.max_rounds,
        episode_budget=EpisodeBudget(args.cli_task_max_turns, args.cli_task_timeout),
        verifier_budget=EpisodeBudget(args.verifier_max_turns, args.verifier_timeout),
        orchestrator_budget=EpisodeBudget(args.orchestrator_max_turns, args.orchestrator_timeout),
        gui_task_budget=EpisodeBudget(args.gui_task_max_turns, args.gui_task_timeout),
        cli_task_budget=EpisodeBudget(args.cli_task_max_turns, args.cli_task_timeout),
        role_verifier_budget=EpisodeBudget(args.verifier_max_turns, args.verifier_timeout),
        workspace_path=workspace,
        harness_dir=harness_dir,
        log_dir=args.log_dir,
        verifier_model=args.verifier_model,
        agent_model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
    )
    env = _build_env(args.env)

    # Each role can run on a different adapter. Defaults keep the CLI simple:
    # one task adapter plus one verifier adapter is enough for Codex/Claude Code.
    agent_cache: dict[tuple[str, str], object] = {}

    def build_role_agent(name: str, *, verifier: bool = False):
        model = args.verifier_model if verifier else args.model
        key = (name, model)
        if key not in agent_cache:
            agent_cache[key] = _build_agent(
                name,
                model=model,
                api_key=args.api_key,
                base_url=args.base_url,
                workspace_path=workspace,
            )
        return agent_cache[key]

    verifier_default = args.verifier_agent or args.agent
    default_agent = build_role_agent(args.agent)
    orchestrator_agent = build_role_agent(args.orchestrator_agent or args.agent)
    gui_task_agent = build_role_agent(args.gui_task_agent or args.agent)
    cli_task_agent = build_role_agent(args.cli_task_agent or args.agent)
    gui_verifier_agent = build_role_agent(args.gui_verifier_agent or verifier_default, verifier=True)
    cli_verifier_agent = build_role_agent(args.cli_verifier_agent or verifier_default, verifier=True)

    from .orchestrator import run

    report = asyncio.run(
        run(
            task=task,
            env=env,
            agent=default_agent,
            orchestrator_agent=orchestrator_agent,
            gui_task_agent=gui_task_agent,
            cli_task_agent=cli_task_agent,
            gui_verifier_agent=gui_verifier_agent,
            cli_verifier_agent=cli_verifier_agent,
            config=config,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("completion_satisfied") else 1


def _read_task(raw: str) -> str:
    if raw.startswith("@"):
        return Path(raw[1:]).read_text(encoding="utf-8").strip()
    return raw


def _build_env(spec: str):
    if spec == "local":
        from .environment.local import LocalEnvironment

        return LocalEnvironment()
    if spec.startswith("ssh://"):
        from .environment.ssh import SSHEnvironment

        return SSHEnvironment.from_url(spec)
    if spec.startswith("docker://"):
        from .environment.docker import DockerEnvironment

        return DockerEnvironment.from_url(spec)
    raise ValueError(f"Unknown env: {spec}")


def _build_agent(name: str, *, model: str, api_key: str | None, base_url: str | None, workspace_path: str):
    if name == "openclaw":
        from .adapters.openclaw import OpenClawAdapter

        return OpenClawAdapter(model=model, api_key=api_key, base_url=base_url, workspace_path=workspace_path)
    if name == "codex":
        from .adapters.codex import CodexAdapter

        return CodexAdapter(model=model, api_key=api_key, base_url=base_url, workspace_path=workspace_path)
    if name == "claude_code":
        from .adapters.claude_code import ClaudeCodeAdapter

        return ClaudeCodeAdapter(model=model, api_key=api_key, base_url=base_url, workspace_path=workspace_path)
    raise ValueError(f"Unknown agent: {name}")


if __name__ == "__main__":
    raise SystemExit(main())
