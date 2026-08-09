"""Drive the real `manager.run` loop with scripted adapters.

Shared by the routing, escalation and briefing tests so they all exercise the
production loop rather than a reimplementation of it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conftest import FakeEnvironment, ScriptedAdapter, audit_report, manager_plan

from lh_harness import manager


@dataclass
class LoopRun:
    report: dict[str, Any]
    manager_agent: ScriptedAdapter
    executors: dict[tuple[str, str], ScriptedAdapter]
    auditors: dict[str, ScriptedAdapter]
    log_dir: Path
    progress: list[tuple[str, dict[str, Any]]]

    def executor(self, executor_type: str, tier: str) -> ScriptedAdapter:
        return self.executors[(executor_type, tier)]

    @property
    def tiers(self) -> list[str]:
        return [item["executor_tier"] for item in self.report["rounds"]]

    def events(self, name: str | None = None) -> list[dict[str, Any]]:
        path = self.log_dir / "role_management" / "events.jsonl"
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [item for item in records if name is None or item.get("event") == name]

    def rounds_jsonl(self) -> list[dict[str, Any]]:
        path = self.log_dir / "role_management" / "rounds.jsonl"
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def round_dir(self, round_index: int) -> Path:
        return self.log_dir / "role_management" / "rounds" / f"round_{round_index:03d}"


def run_loop(
    config,
    *,
    plans: list[str],
    audits: list[str],
    task: str = "Ship the feature",
) -> LoopRun:
    """Run the loop with one scripted adapter per role and (type, tier) cell.

    `plans` are the manager's replies in order; `audits` are the auditor's. Both
    auditors share the audit script, so a test only has to describe the verdict
    sequence, not which auditor produced it.
    """
    manager_agent = ScriptedAdapter("manager", plans, default=manager_plan("blocked"))
    executors = {
        (executor_type, tier): ScriptedAdapter(
            f"{executor_type}/{tier}", default=f"{executor_type}/{tier} executor ran"
        )
        for executor_type in ("gui", "cli")
        for tier in ("cheap", "strong")
    }
    remaining = list(audits)

    class SharedAuditor(ScriptedAdapter):
        async def run_episode(self, prompt, env, budget, live_trajectory_path=None):
            self.replies = [remaining.pop(0)] if remaining else []
            return await super().run_episode(prompt, env, budget, live_trajectory_path)

    auditors = {
        "gui": SharedAuditor("gui_auditor", default=audit_report(passing=False)),
        "cli": SharedAuditor("cli_auditor", default=audit_report(passing=False)),
    }
    progress: list[tuple[str, dict[str, Any]]] = []

    report = asyncio.run(
        manager.run(
            task=task,
            env=FakeEnvironment(),
            config=config,
            manager_agent=manager_agent,
            executor_agents={
                executor_type: {
                    tier: executors[(executor_type, tier)] for tier in ("cheap", "strong")
                }
                for executor_type in ("gui", "cli")
            },
            gui_executor_agent=executors[("gui", "cheap")],
            cli_executor_agent=executors[("cli", "cheap")],
            gui_auditor_agent=auditors["gui"],
            cli_auditor_agent=auditors["cli"],
            final_response_agent=ScriptedAdapter("final_response", default="Here is what happened."),
            progress=lambda event, payload: progress.append((event, payload)),
        )
    )
    return LoopRun(
        report=report,
        manager_agent=manager_agent,
        executors=executors,
        auditors=auditors,
        log_dir=Path(config.log_dir),
        progress=progress,
    )


def executor_calls(run: LoopRun) -> dict[str, int]:
    """Non-zero executor call counts, keyed `type/tier`."""
    return {
        f"{executor_type}/{tier}": adapter.calls
        for (executor_type, tier), adapter in run.executors.items()
        if adapter.calls
    }
