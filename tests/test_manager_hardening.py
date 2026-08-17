from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lh_harness.dashboard.state import DashboardState
from lh_harness.environment.local import LocalEnvironment
from lh_harness.manager import (
    _GateContext,
    _human_gate,
    _merge_episode_logs,
    _save_role_result,
    _write_terminal_failure,
    run,
)
from lh_harness.types import EpisodeBudget, EpisodeResult, HarnessConfig
from lh_harness.utils.run_boundary import safe_run_logs, safe_run_role


def test_terminal_failure_does_not_follow_role_directory_symlink(tmp_path: Path) -> None:
    log_dir = tmp_path / "lh_harness"
    outside = tmp_path / "outside-role"
    log_dir.mkdir()
    outside.mkdir()
    outside_report = outside / "report.json"
    outside_events = outside / "events.jsonl"
    outside_report.write_text("private report", encoding="utf-8")
    outside_events.write_text("private events", encoding="utf-8")
    (log_dir / "role_orchestration").symlink_to(outside, target_is_directory=True)

    result = _write_terminal_failure(
        SimpleNamespace(log_dir=str(log_dir), max_total_episodes=5),
        "task",
        status="failed",
        reason="worker crashed",
        exc=RuntimeError("boom"),
        abort_reason="worker_exception",
    )

    assert result["status"] == "failed"
    assert json.loads((log_dir / "report.json").read_text(encoding="utf-8"))["status"] == "failed"
    assert outside_report.read_text(encoding="utf-8") == "private report"
    assert outside_events.read_text(encoding="utf-8") == "private events"


def test_terminal_failure_does_not_follow_log_directory_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside-logs"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("private", encoding="utf-8")
    log_link = tmp_path / "lh_harness"
    log_link.symlink_to(outside, target_is_directory=True)

    result = _write_terminal_failure(
        SimpleNamespace(log_dir=str(log_link), max_total_episodes=5),
        "task",
        status="failed",
        reason="worker crashed",
        exc=RuntimeError("boom"),
        abort_reason="worker_exception",
    )

    assert result["status"] == "failed"
    assert sentinel.read_text(encoding="utf-8") == "private"
    assert not (outside / "report.json").exists()
    assert not (outside / "role_orchestration").exists()


def test_role_result_writes_osworld_style_episode_with_dashboard_role_names(tmp_path: Path) -> None:
    result_dir = tmp_path / "run-1"
    log_dir = result_dir / "lh_harness"
    round_dir = log_dir / "role_orchestration" / "rounds" / "round_001"
    round_dir.mkdir(parents=True)
    raw = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "message-1", "type": "agent_message", "text": "finished"},
                }
            ),
            json.dumps({"type": "turn.completed"}),
        )
    )
    result = EpisodeResult(
        status="done",
        actions_log=raw,
        duration_ms=123,
        metadata={"command": "codex exec --json", "exit_code": 0},
    )
    screenshot = b"\x89PNG\r\n\x1a\nfinal"

    artifacts = _save_role_result(
        round_dir,
        "executor",
        result,
        episode_root=log_dir / "cli_executor_episodes",
        final_screenshot=screenshot,
    )
    _merge_episode_logs(log_dir)

    episode = log_dir / "cli_executor_episodes" / "ep001"
    assert (episode / "codex_stream.jsonl").read_text(encoding="utf-8") == raw
    assert "assistant output:\nfinished" in (episode / "agent.log").read_text(encoding="utf-8")
    chat = [json.loads(line) for line in (episode / "chat.jsonl").read_text(encoding="utf-8").splitlines()]
    assert chat[0]["type"] == "session"
    assert chat[-1]["message"]["content"][0]["text"] == "finished"
    assert (episode / "final_screenshot.png").read_bytes() == screenshot
    assert (round_dir / "executor_raw_trajectory.jsonl").read_text(encoding="utf-8") == raw
    assert (round_dir / "executor_metadata.json").is_file()
    assert not (round_dir / "task_raw_trajectory.txt").exists()
    assert not (round_dir / "task_metadata.json").exists()
    assert artifacts["screenshot_count"] == 1
    manifest = json.loads((round_dir / "executor_screenshots.json").read_text(encoding="utf-8"))
    assert manifest["screenshots"][0]["kind"] == "final_environment_screenshot"
    assert "cli_executor_episodes/ep001" in (result_dir / "agent.log").read_text(encoding="utf-8")
    assert "finished" in (result_dir / "chat.jsonl").read_text(encoding="utf-8")
    listed = DashboardState(log_dir).list_round_artifacts(1)
    assert "executor_raw_trajectory.jsonl" in listed
    assert "executor_metadata.json" in listed


def test_short_lived_cua_result_layout_remains_readable(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "old-cua-run"
    role_dir = run_dir / "cua_harness" / "role_orchestration"
    (role_dir / "rounds").mkdir(parents=True)

    assert safe_run_logs(runs_root, run_dir, allow_missing=False) == run_dir / "cua_harness"
    assert safe_run_role(runs_root, run_dir, allow_missing=False) == role_dir


@pytest.mark.asyncio
async def test_dashboard_instructions_are_kept_for_manager_and_final_reply(tmp_path: Path) -> None:
    async def human_hook(_context: dict[str, object]) -> dict[str, object]:
        return {
            "action": "continue",
            "instructions": "The final reply must contain OPERATOR_MARKER.",
        }

    ctx = _GateContext(
        config=SimpleNamespace(max_total_episodes=2),
        task="original task",
        rounds=[],
        human_hook=human_hook,
        log_dir=tmp_path,
        events_path=tmp_path / "events.jsonl",
        round_budget=2,
    )

    should_stop = await _human_gate(ctx, "progress", 1, "verified state")

    assert should_stop is False
    assert ctx.carryover_instructions == "The final reply must contain OPERATOR_MARKER."
    assert ctx.operator_instructions == ["The final reply must contain OPERATOR_MARKER."]


@pytest.mark.asyncio
async def test_provider_failure_stops_without_round_limit_approval(tmp_path: Path) -> None:
    message = "The 'bad-model' model is not supported when using Codex with a ChatGPT account."

    class FailingAgent:
        async def run_episode(self, _prompt, _env, _budget, live_trajectory_path=None):
            return EpisodeResult(
                status="error",
                actions_log=json.dumps({"type": "turn.failed", "error": {"message": message}}),
                metadata={
                    "runtime_signals": [
                        {"signal": "AGENT_TURN_FAILED", "evidence": f"AGENT_TURN_FAILED: {message}"}
                    ]
                },
            )

    approvals: list[dict[str, object]] = []

    async def human_hook(context: dict[str, object]) -> dict[str, object]:
        approvals.append(context)
        return {"action": "continue"}

    config = HarnessConfig(
        max_total_episodes=30,
        manager_budget=EpisodeBudget(max_duration_seconds=10),
        workspace_path=str(tmp_path / "workspace"),
        harness_dir=str(tmp_path / "harness"),
        log_dir=str(tmp_path / "logs"),
    )
    result = await run(
        task="test invalid provider model",
        env=LocalEnvironment(str(tmp_path / "tmp")),
        config=config,
        agent=FailingAgent(),
        human_hook=human_hook,
    )

    assert approvals == []
    assert result["status"] == "failed"
    assert result["rounds_run"] == 1
    assert result["abort_reason"] == "provider_model_unavailable"
    assert message in result["failure_reason"]
    assert result["rounds"][0]["manager_status"]["error"] == f"模型不可用：{message}"
    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "role_orchestration" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    failure = next(event for event in events if event["event"] == "agent_runtime_failed")
    assert failure["status"] == "failed"
    assert failure["episode_status"]["status"] == "error"
    assert failure["episode_status"]["error"] == f"模型不可用：{message}"


@pytest.mark.asyncio
async def test_late_crash_report_preserves_completed_rounds(tmp_path: Path) -> None:
    outputs = iter(
        (
            "Next: cli\n\nCurrent Task State:\nwork pending",
            "executor finished the requested work",
            "Status: complete\nIntegrity: clean\nContract audit: aligned",
            "Next: done\n\nCurrent Task State:\nwork finished",
            "The requested work is complete.",
        )
    )

    class SequencedAgent:
        async def run_episode(self, _prompt, _env, _budget, live_trajectory_path=None):
            return EpisodeResult(status="done", actions_log=next(outputs))

    async def crashing_human_hook(context: dict[str, object]) -> dict[str, object]:
        if context.get("outcome") == "completed":
            raise RuntimeError("dashboard gate crashed after completion")
        return {"action": "continue"}

    log_dir = tmp_path / "logs"
    config = HarnessConfig(
        max_total_episodes=2,
        manager_budget=EpisodeBudget(max_duration_seconds=10),
        workspace_path=str(tmp_path / "workspace"),
        harness_dir=str(tmp_path / "harness"),
        log_dir=str(log_dir),
    )

    result = await run(
        task="finish two managed rounds",
        env=LocalEnvironment(str(tmp_path / "tmp")),
        config=config,
        agent=SequencedAgent(),
        human_hook=crashing_human_hook,
    )

    assert result["status"] == "failed"
    assert result["abort_reason"] == "worker_exception"
    assert result["exception_type"] == "RuntimeError"
    assert result["rounds_run"] == 2
    assert [item["round_index"] for item in result["rounds"]] == [1, 2]
    assert result["completion_satisfied"] is True
    assert result["current_task_state"].endswith("work finished")
    assert result["latest_auditor_report"].startswith("Status: complete")
    assert result["final_response"] == "The requested work is complete."
    assert result["elapsed_seconds"] >= 0

    persisted = json.loads((log_dir / "report.json").read_text(encoding="utf-8"))
    assert persisted == result
