"""The resumed manager loop continues numbering and sees its own history."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from lh_harness.environment.local import LocalEnvironment
from lh_harness.manager import run
from lh_harness.types import EpisodeResult, HarnessConfig, ManagedRound

# The manager reply that routes one more CLI executor round, and the reply that
# claims completion.  Completion is only accepted when the previous auditor
# report was clean, so a resumed ledger must supply that evidence.
_PLAN = "Next: cli\n\nCurrent Task State:\nstill working\n\nPlan:\nkeep going"
_DONE = "Next: done\n\nCurrent Task State:\nall finished"
_EXECUTED = "executor made the change"


def _clean_audit(summary: str) -> str:
    return (
        "Status: complete\n"
        "Integrity: clean\n"
        "Contract audit: aligned\n\n"
        f"Summary:\n{summary}"
    )


_CLEAN_AUDIT = _clean_audit("the change is verified")


def _ledger_round(index: int, **overrides: Any) -> dict[str, Any]:
    record = ManagedRound(
        round_index=index,
        next_step="cli",
        plan_text=f"plan {index}",
        executor_output=f"output {index}",
        auditor_report=_clean_audit(f"round {index} verified"),
        harness_feedback="",
        task_state=f"state {index}",
        task_contract=f"contract {index}",
        related_report_refs=[],
        manager_status={},
        executor_status={},
        auditor_status={},
    )
    return {**asdict(record), **overrides}


def _seed_ledger(tmp_path: Path, indices: list[int]) -> Path:
    role = tmp_path / "logs" / "role_orchestration"
    role.mkdir(parents=True, exist_ok=True)
    with (role / "rounds.jsonl").open("w", encoding="utf-8") as handle:
        for index in indices:
            handle.write(json.dumps(_ledger_round(index), ensure_ascii=False) + "\n")
    return role


class SequencedAgent:
    """Replays a fixed reply sequence and records the prompts it received."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    async def run_episode(self, prompt, _env, _budget, live_trajectory_path=None):
        self.prompts.append(str(prompt))
        reply = self._replies.pop(0) if self._replies else _DONE
        return EpisodeResult(status="done", actions_log=reply)


async def _run(
    tmp_path: Path,
    *,
    resume: bool,
    replies: list[str],
    max_rounds: int = 2,
    pending_instructions=None,
) -> tuple[dict[str, Any], SequencedAgent]:
    agent = SequencedAgent(replies)
    report = await run(
        task="finish the refactor",
        env=LocalEnvironment(str(tmp_path / "tmp")),
        config=HarnessConfig(
            max_total_episodes=max_rounds,
            workspace_path=str(tmp_path / "workspace"),
            harness_dir=str(tmp_path / "harness"),
            log_dir=str(tmp_path / "logs"),
        ),
        agent=agent,
        resume=resume,
        pending_instructions=pending_instructions,
    )
    return report, agent


@pytest.mark.asyncio
async def test_a_resumed_loop_continues_the_round_numbering(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, [1, 2, 3])

    report, _agent = await _run(tmp_path, resume=True, replies=[_DONE])

    assert [item["round_index"] for item in report["rounds"]] == [1, 2, 3, 4], (
        "the loop must continue at 4, not restart at 1"
    )
    assert report["rounds_run"] == 4
    # max_total_episodes is the *additional* budget after a resume.
    assert report["max_rounds"] == 5
    rounds_dir = tmp_path / "logs" / "role_orchestration" / "rounds"
    assert (rounds_dir / "round_004").is_dir()
    assert not (rounds_dir / "round_001").exists(), "restored rounds must not re-run"


@pytest.mark.asyncio
async def test_a_resumed_loop_feeds_the_history_to_the_manager(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, [1, 2])

    _report, agent = await _run(tmp_path, resume=True, replies=[_DONE])

    assert agent.prompts, "the manager must be asked for the next plan"
    prompt = agent.prompts[0]
    assert "state 2" in prompt, "the restored task state must reach the prompt"
    assert "contract 2" in prompt, "the restored contract must reach the prompt"
    assert "round 2 verified" in prompt, "the restored auditor report must reach the prompt"


@pytest.mark.asyncio
async def test_a_resumed_loop_accepts_completion_grounded_in_restored_history(
    tmp_path: Path,
) -> None:
    # The restored ledger's clean auditor report is the evidence that makes an
    # immediate ``Next: done`` acceptable; without it the manager is sent back
    # for repair.  This proves the restored rounds are semantically live.
    _seed_ledger(tmp_path, [1, 2])

    report, _agent = await _run(tmp_path, resume=True, replies=[_DONE])

    assert report["completion_satisfied"] is True
    assert report["status"] == "complete"


@pytest.mark.asyncio
async def test_a_message_sent_before_a_stop_reaches_the_very_next_round(tmp_path: Path) -> None:
    # The operator typed this while round 2 was running, then stopped the run.
    # It was never handed to a gate, so only a round-start claim can deliver it
    # to round 3 instead of the round after that.
    _seed_ledger(tmp_path, [1, 2])
    queued = [["check the config file first"]]

    _report, agent = await _run(
        tmp_path,
        resume=True,
        replies=[_DONE],
        pending_instructions=lambda: queued.pop(0) if queued else [],
    )

    assert "check the config file first" in agent.prompts[0]
    assert (
        tmp_path / "logs" / "role_orchestration" / "rounds" / "round_003" / "human_instructions.txt"
    ).read_text(encoding="utf-8") == "check the config file first"


@pytest.mark.asyncio
async def test_a_resumed_loop_records_the_resume_in_its_events(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, [1, 2, 3])

    await _run(tmp_path, resume=True, replies=[_DONE])

    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "role_orchestration" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    start = next(item for item in events if item["event"] == "role_harness_start")
    resumed = next(item for item in events if item["event"] == "role_harness_resumed")
    assert start["resumed"] is True
    assert start["resumed_rounds"] == 3
    assert start["max_rounds"] == 5
    assert resumed["restored_rounds"] == 3
    assert resumed["resume_from_round"] == 3
    assert resumed["round_budget"] == 5


@pytest.mark.asyncio
async def test_a_resumed_loop_appends_to_the_existing_ledger(tmp_path: Path) -> None:
    role = _seed_ledger(tmp_path, [1, 2])

    await _run(tmp_path, resume=True, replies=[_DONE])

    recorded = [
        json.loads(line)
        for line in (role / "rounds.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [item["round_index"] for item in recorded] == [1, 2, 3]


@pytest.mark.asyncio
async def test_without_resume_an_existing_ledger_is_ignored(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, [1, 2, 3])

    report, agent = await _run(tmp_path, resume=False, replies=[_DONE])

    assert report["rounds"][0]["round_index"] == 1
    assert report["max_rounds"] == 2
    assert "state 3" not in agent.prompts[0], "a plain start must not inherit history"
    # Without the restored clean audit there is no evidence for completion, so
    # the immediate ``Next: done`` is rejected and repaired instead.
    assert report["completion_satisfied"] is False


@pytest.mark.asyncio
async def test_a_resume_with_no_ledger_starts_from_round_one(tmp_path: Path) -> None:
    report, _agent = await _run(tmp_path, resume=True, replies=[_DONE])

    assert report["rounds"][0]["round_index"] == 1
    assert report["max_rounds"] == 2


@pytest.mark.asyncio
async def test_a_resumed_loop_still_stops_at_its_budget(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, [1, 2, 3])

    report, _agent = await _run(
        tmp_path,
        resume=True,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _PLAN, _EXECUTED, _CLEAN_AUDIT, _PLAN],
    )

    # 3 restored + 2 additional rounds, then the budget is exhausted.
    assert [item["round_index"] for item in report["rounds"]] == [1, 2, 3, 4, 5]
    assert report["max_rounds"] == 5
    assert report["completion_satisfied"] is False
