from __future__ import annotations

import json
from pathlib import Path

import pytest

from lh_harness import cli
from lh_harness.config import ProjectConfigError, load_run_defaults
from lh_harness.environment.local import LocalEnvironment
from lh_harness.manager import run
from lh_harness.types import EpisodeBudget, EpisodeResult, HarnessConfig


def test_final_response_timeout_config_and_cli_override(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".lh-harness" / "config.toml"
    config.parent.mkdir()
    config.write_text("[run.timeouts]\nfinal_response = 1200\n", encoding="utf-8")
    assert load_run_defaults(config)["final_response_timeout"] == 1200
    captured = []
    monkeypatch.setattr(cli, "_run_command", lambda args: captured.append(args) or 0)
    assert cli.main(["run", "--task", "test"]) == 0
    assert captured[-1].final_response_timeout == 1200
    assert cli.main(["run", "--task", "test", "--final-response-timeout", "900"]) == 0
    assert captured[-1].final_response_timeout == 900


@pytest.mark.parametrize("value", ["0", "-1", "true", "1.5", '"slow"'])
def test_final_response_timeout_rejects_invalid_config(tmp_path, value) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f"[run.timeouts]\nfinal_response = {value}\n", encoding="utf-8")
    with pytest.raises(ProjectConfigError):
        load_run_defaults(config)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [None, 1200])
@pytest.mark.parametrize("failure", ["timeout", "exception"])
async def test_final_response_failure_preserves_completed_report(tmp_path, timeout, failure) -> None:
    outputs = iter([
        "Next: cli\n\nCurrent Task State:\nwork pending",
        "executor finished the requested work",
        "Status: complete\nIntegrity: clean\nContract audit: aligned",
        "Next: done\n\nCurrent Task State:\nwork finished",
    ])

    class WorkAgent:
        async def run_episode(self, prompt, env, budget, live_trajectory_path=None):
            return EpisodeResult(status="done", actions_log=next(outputs))

    budgets = []

    class ReplyAgent:
        async def run_episode(self, prompt, env, budget, live_trajectory_path=None):
            budgets.append(budget.max_duration_seconds)
            if failure == "exception":
                raise RuntimeError("reply provider failed")
            return EpisodeResult(status="timeout", error="reply timed out")

    config = HarnessConfig(
        max_total_episodes=2,
        workspace_path=str(tmp_path / "workspace"),
        harness_dir=str(tmp_path / "harness"),
        log_dir=str(tmp_path / "logs"),
    )
    if timeout is not None:
        config.final_response_budget = EpisodeBudget(max_duration_seconds=timeout)
    report = await run(
        task="finish the requested work", env=LocalEnvironment(str(tmp_path / "tmp")),
        config=config, agent=WorkAgent(), final_response_agent=ReplyAgent(),
    )
    assert budgets == [timeout or 180]
    assert report["status"] == "complete"
    assert report["completion_satisfied"] is True
    assert report["rounds_run"] == 2
    assert report["final_response"] == ""
    assert json.loads((Path(config.log_dir) / "report.json").read_text()) == report
