"""Test doubles for the role-management loop.

Nothing here calls a real agent CLI or a paid model. The plan and audit text the
builders produce is parsed by the production parsers, so a routing test fails if
the real protocol changes, not only if a stand-in does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from lh_harness.types import EpisodeBudget, EpisodeResult, ExecResult, HarnessConfig


@dataclass
class RecordedEpisode:
    prompt: str
    budget_seconds: int
    live_trajectory_path: str | None


class ScriptedAdapter:
    """An `AgentAdapter` that replays queued replies and records its prompts.

    `label` identifies which adapter ran, which is the whole point in a routing
    test: asserting that the cheap cell was used and the strong one was not.
    """

    def __init__(self, label: str, replies: list[str] | None = None, *, default: str = "") -> None:
        self.label = label
        self.replies = list(replies or [])
        self.default = default
        self.episodes: list[RecordedEpisode] = []

    @property
    def calls(self) -> int:
        return len(self.episodes)

    @property
    def prompts(self) -> list[str]:
        return [episode.prompt for episode in self.episodes]

    async def run_episode(
        self,
        prompt: str,
        env: Any,
        budget: EpisodeBudget,
        live_trajectory_path: str | None = None,
    ) -> EpisodeResult:
        self.episodes.append(
            RecordedEpisode(prompt, budget.max_duration_seconds, live_trajectory_path)
        )
        text = self.replies.pop(0) if self.replies else self.default
        return EpisodeResult(
            status="done",
            actions_log="",
            duration_ms=1,
            # First key in VISIBLE_OUTPUT_KEYS, so the loop reads this verbatim
            # instead of trying to decode a Claude/Codex trajectory.
            metadata={"assistant_visible_output": text, "exit_code": 0},
        )


@dataclass
class FakeEnvironment:
    """In-memory `Environment`; the loop's remote mirroring is a no-op here."""

    staging_dir: str | None = None
    written: dict[str, str] = field(default_factory=dict)

    async def exec(self, command: str, timeout: int = 30, tee_path: str | None = None) -> ExecResult:
        return ExecResult(stdout="", stderr="", exit_code=0, duration_ms=0)

    async def screenshot(self) -> bytes:
        return b""

    async def upload(self, local_path: str, remote_path: str) -> None:
        self.written[remote_path] = local_path

    async def download(self, remote_path: str, local_path: str) -> None:
        raise NotImplementedError


def manager_plan(
    route: str,
    *,
    tier: str | None = None,
    task: str = "Do the next piece of work",
    related: str = "",
    state: str = "Completed: nothing yet.",
) -> str:
    """A manager reply the real `parse_role_manager_*` helpers accept."""
    lines = [
        "Current task state:",
        state,
        "",
        "Task contract:",
        "Produce the requested state.",
        "",
        "Dependency assessment:",
        "Target state: the deliverable. Routing rationale: direct.",
        "",
        f"Next: {route}",
    ]
    if tier is not None:
        lines.append(f"Executor tier: {tier}")
    if route in {"gui", "cli"}:
        lines += ["", f"Task: {task}", "", f"Related audit reports: {related or 'none'}"]
    return "\n".join(lines) + "\n"


def audit_report(*, passing: bool, detail: str = "") -> str:
    """An auditor reply the real `parse_audit_report` accepts.

    A passing report deliberately carries no `Blocking constraints:` entries, so
    it survives the acceptance-constraint guard and reads as complete.
    """
    if passing:
        head = ["Status: complete", "Integrity: clean", "Contract audit: aligned"]
        body = detail or "The subtask produced the required persisted state."
        backcheck = ["Contract conclusion: aligned", "Blocking constraints: none"]
    else:
        head = ["Status: incomplete", "Integrity: suspect", "Contract audit: unknown"]
        body = detail or "The required state was never persisted."
        backcheck = ["Contract conclusion: unknown", "Blocking constraints: the deliverable is missing"]
    return "\n".join(
        [
            *head,
            "",
            f"Audit facts: {body}",
            "",
            "Acceptance-constraint backcheck:",
            *backcheck,
            "",
            "State update for manager:",
            body,
        ]
    ) + "\n"


def progress_report(gap: str = "the remaining step") -> str:
    """A clean, contract-aligned round that simply is not the last one.

    This is what real auditors return for ordinary mid-run progress -- the E2E
    runs produced exactly `incomplete / clean / aligned`. It must never count as
    an escalation-worthy failure.
    """
    return "\n".join(
        [
            "Status: incomplete",
            "Integrity: clean",
            "Contract audit: aligned",
            "",
            "Audit facts: the subtask completed correctly; the overall contract is not finished.",
            "",
            "Acceptance-constraint backcheck:",
            "Contract conclusion: aligned",
            "Blocking constraints: none",
            "",
            "State update for manager:",
            f"Still outstanding: {gap}.",
        ]
    ) + "\n"


@pytest.fixture
def harness_config(tmp_path):
    """A config whose every path is inside tmp_path, with short budgets."""

    def build(**overrides: Any) -> HarnessConfig:
        defaults: dict[str, Any] = {
            "max_total_episodes": 6,
            "manager_budget": EpisodeBudget(max_duration_seconds=5),
            "gui_executor_budget": EpisodeBudget(max_duration_seconds=7),
            "cli_executor_budget": EpisodeBudget(max_duration_seconds=9),
            "auditor_budget": EpisodeBudget(max_duration_seconds=5),
            "workspace_path": str(tmp_path / "workspace"),
            "harness_dir": str(tmp_path / "harness"),
            "log_dir": str(tmp_path / "logs"),
        }
        defaults.update(overrides)
        return HarnessConfig(**defaults)

    return build
