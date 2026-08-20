"""Resuming an interrupted run continues its ledger instead of restarting."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

import lh_harness.cli as cli
from lh_harness.dashboard.gate import make_human_hook
from lh_harness.dashboard.state import DashboardState, _normalise_extra_rounds
from lh_harness.manager import _extra_rounds, _managed_round_from_dict, _recorded_rounds
from lh_harness.supervisor.lifecycle import ACTIVE_STATUSES, resume_epoch
from lh_harness.supervisor.service import (
    RunSupervisor,
    _lifecycle_command_id,
    _merge_lifecycle_status,
    _resume_round_budget,
)
from lh_harness.types import MAX_ROUNDS, ManagedRound


class FakeProcess:
    pid = 4242
    returncode = None

    def poll(self):
        return self.returncode


def _round(index: int, **overrides: Any) -> dict[str, Any]:
    record = ManagedRound(
        round_index=index,
        next_step="cli",
        plan_text=f"plan {index}",
        executor_output=f"output {index}",
        auditor_report=f"report {index}",
        harness_feedback="",
        task_state=f"state {index}",
        task_contract=f"contract {index}",
        related_report_refs=[],
        manager_status={"ok": True},
        executor_status={},
        auditor_status={},
    )
    return {**asdict(record), **overrides}


def _write_ledger(role_dir: Path, records: list[dict[str, Any]]) -> None:
    role_dir.mkdir(parents=True, exist_ok=True)
    with (role_dir / "rounds.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# --- manager ledger restore -------------------------------------------------


def test_recorded_rounds_restores_contiguous_history(tmp_path: Path) -> None:
    role = tmp_path / "role_orchestration"
    _write_ledger(role, [_round(1), _round(2), _round(3)])

    restored = _recorded_rounds(role)

    assert [item.round_index for item in restored] == [1, 2, 3]
    assert restored[-1].task_state == "state 3"
    assert restored[-1].task_contract == "contract 3"
    assert restored[0].plan_text == "plan 1"


def test_recorded_rounds_keeps_the_latest_entry_per_index(tmp_path: Path) -> None:
    role = tmp_path / "role_orchestration"
    # The loop re-records a round after a late gate decision; the ledger is
    # append-only, so the newest entry for an index must win.
    _write_ledger(role, [_round(1), _round(1, plan_text="revised plan")])

    restored = _recorded_rounds(role)

    assert len(restored) == 1
    assert restored[0].plan_text == "revised plan"


def test_recorded_rounds_skips_a_truncated_tail(tmp_path: Path) -> None:
    role = tmp_path / "role_orchestration"
    role.mkdir(parents=True)
    good = json.dumps(_round(1), ensure_ascii=False)
    (role / "rounds.jsonl").write_text(good + "\n" + good[: len(good) // 2], encoding="utf-8")

    restored = _recorded_rounds(role)

    assert [item.round_index for item in restored] == [1]


@pytest.mark.parametrize(
    "payload",
    [
        {"round_index": 0},
        {"round_index": -1},
        {"round_index": True},
        {"round_index": "2"},
        {"round_index": MAX_ROUNDS + 1},
        {"no_index": 1},
        [1, 2, 3],
        "not an object",
    ],
)
def test_recorded_rounds_rejects_unusable_entries(tmp_path: Path, payload: Any) -> None:
    role = tmp_path / "role_orchestration"
    role.mkdir(parents=True)
    (role / "rounds.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")

    assert _recorded_rounds(role) == []


def test_recorded_rounds_is_empty_without_a_ledger(tmp_path: Path) -> None:
    assert _recorded_rounds(tmp_path / "missing") == []


def test_managed_round_from_dict_sanitises_untrusted_fields() -> None:
    record = _managed_round_from_dict(
        {
            "round_index": 2,
            "next_step": "not-a-route",
            "plan_text": None,
            "related_report_refs": ["a", 7, None],
            "manager_status": "not-a-dict",
        }
    )

    assert record.next_step == "invalid"
    assert record.plan_text == ""
    assert record.related_report_refs == ["a"]
    assert record.manager_status == {}


# --- resume round budget ----------------------------------------------------


def test_resume_round_budget_prefers_the_operator_value() -> None:
    assert _resume_round_budget({"max_rounds": 25}, 4) == 4


def test_resume_round_budget_falls_back_to_the_saved_value() -> None:
    assert _resume_round_budget({"max_rounds": 7}, None) == 7


@pytest.mark.parametrize("saved", [0, -1, MAX_ROUNDS + 1, "many", None, True])
def test_resume_round_budget_ignores_an_unusable_saved_value(saved: Any) -> None:
    assert _resume_round_budget({"max_rounds": saved}, None) >= 1


@pytest.mark.parametrize("extra", [0, -1, MAX_ROUNDS + 1, True, "3", 1.5])
def test_resume_round_budget_rejects_an_invalid_operator_value(extra: Any) -> None:
    with pytest.raises(ValueError):
        _resume_round_budget({"max_rounds": 5}, extra)


# --- lifecycle epoch --------------------------------------------------------


def test_lifecycle_command_id_is_unsuffixed_for_the_first_generation() -> None:
    assert _lifecycle_command_id("stop", 0) == "lifecycle-stop"
    assert _lifecycle_command_id("stop", 1) == "lifecycle-stop@1"
    assert _lifecycle_command_id("abort", 2) == "lifecycle-abort@2"


@pytest.mark.parametrize(
    "value,expected",
    [({}, 0), ({"resume_epoch": 3}, 3), ({"resume_epoch": -1}, 0), ({"resume_epoch": True}, 0), ({"resume_epoch": "2"}, 0), (None, 0)],
)
def test_resume_epoch_reads_only_a_sane_integer(value: Any, expected: int) -> None:
    assert resume_epoch(value) == expected


def test_a_new_epoch_supersedes_the_previous_terminal_status() -> None:
    current = {
        "status": "cancelled",
        "alive": False,
        "finished_at": 100.0,
        "requested_action": "stop",
        "stop_requested_at": 99.0,
        "exit_code": -15,
    }
    candidate = {"status": "creating", "alive": False, "resume_epoch": 1}

    merged = _merge_lifecycle_status(current, candidate)

    assert merged["status"] == "creating"
    assert merged["resume_epoch"] == 1
    for stale in ("finished_at", "requested_action", "stop_requested_at", "exit_code"):
        assert stale not in merged


def test_the_same_epoch_still_freezes_a_terminal_status() -> None:
    current = {"status": "cancelled", "alive": False, "resume_epoch": 1, "finished_at": 5.0}
    candidate = {"status": "running", "alive": True, "resume_epoch": 1}

    merged = _merge_lifecycle_status(current, candidate)

    assert merged["status"] == "cancelled"
    assert merged["alive"] is False


def test_creating_is_an_active_status() -> None:
    # A run that died inside the launch transaction must reconcile as an
    # interrupted active run rather than a state that is neither active nor
    # terminal (which left it unresumable).
    assert "creating" in ACTIVE_STATUSES


# --- supervisor resume ------------------------------------------------------


def _terminal_run(supervisor: RunSupervisor, run_dir: Path, run_id: str, **owner_extra: Any) -> Path:
    role = run_dir / "lh_harness" / "role_orchestration"
    (run_dir / "control").mkdir(parents=True, exist_ok=True)
    (run_dir / "tmp").mkdir(parents=True, exist_ok=True)
    _write_ledger(role, [_round(1), _round(2)])
    owner = {
        "run_id": run_id,
        "state": "cancelled",
        "task": "keep going",
        "agent": "codex",
        "model": "gpt-5.5-codex",
        "max_rounds": 5,
        "prompt_language": "zh",
        "workspace": str(supervisor.workspace_root),
        "pid": 999_999,
        "pgid": 999_999,
        "exit_code": 143,
        "requested_action": "stop",
        "command": ["stale"],
        "command_display": "stale",
        **owner_extra,
    }
    (run_dir / "control" / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
    (run_dir / "control" / "status.json").write_text(
        json.dumps({
            "run_id": run_id,
            "status": "cancelled",
            "alive": False,
            "finished_at": time.time(),
            "requested_action": "stop",
        }),
        encoding="utf-8",
    )
    return role


@pytest.fixture()
def supervisor(monkeypatch, tmp_path: Path) -> RunSupervisor:
    monkeypatch.setattr(
        "lh_harness.supervisor.service.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr("lh_harness.supervisor.service.os.killpg", lambda *args, **kwargs: None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return RunSupervisor(tmp_path / "runs", workspace_root=workspace)


def test_continue_reuses_the_run_and_passes_resume_to_the_worker(supervisor: RunSupervisor) -> None:
    run_id = "20990101T000000Z_keepgoing"
    run_dir = supervisor.runs_root / run_id
    _terminal_run(supervisor, run_dir, run_id)

    result = supervisor.resume(run_id, mode="continue")

    assert result["id"] == run_id
    owner = result["owner"]
    assert owner["resume_kind"] == "continue"
    assert owner["resume_epoch"] == 1
    assert "--resume" in owner["command"]
    assert f"--max-rounds={5}" in owner["command"]
    # The previous generation's outcome must not survive into the new owner.
    for stale in ("exit_code", "requested_action"):
        assert stale not in owner


def test_continue_bumps_the_epoch_on_each_resume(supervisor: RunSupervisor) -> None:
    run_id = "20990101T000000Z_twice"
    run_dir = supervisor.runs_root / run_id
    _terminal_run(supervisor, run_dir, run_id)

    first = supervisor.resume(run_id, mode="continue")
    assert first["owner"]["resume_epoch"] == 1

    # Mark it terminal again, as a real stopped worker would.
    supervisor._bus(run_id).write_status({
        "run_id": run_id,
        "status": "cancelled",
        "alive": False,
        "resume_epoch": 1,
    })
    second = supervisor.resume(run_id, mode="continue")

    assert second["owner"]["resume_epoch"] == 2


def test_continue_honours_an_explicit_extra_round_grant(supervisor: RunSupervisor) -> None:
    run_id = "20990101T000000Z_extra"
    run_dir = supervisor.runs_root / run_id
    _terminal_run(supervisor, run_dir, run_id)

    result = supervisor.resume(run_id, mode="continue", extra_rounds=3)

    assert "--max-rounds=3" in result["owner"]["command"]


def test_retry_still_creates_a_separate_run(supervisor: RunSupervisor) -> None:
    run_id = "20990101T000000Z_retry"
    run_dir = supervisor.runs_root / run_id
    _terminal_run(supervisor, run_dir, run_id)

    result = supervisor.resume(run_id, mode="retry")

    assert result["id"] != run_id
    assert result["owner"]["resume_kind"] == "retry"
    assert result["owner"]["resumed_from"] == run_id
    assert "--resume" not in result["owner"]["command"]


def test_resume_rejects_an_unknown_mode(supervisor: RunSupervisor) -> None:
    with pytest.raises(ValueError, match="continue or retry"):
        supervisor.resume("whatever", mode="rewind")


@pytest.mark.parametrize("extra", [0, -1, MAX_ROUNDS + 1, True, "2"])
def test_resume_rejects_an_out_of_range_grant(supervisor: RunSupervisor, extra: Any) -> None:
    with pytest.raises(ValueError, match="extra_rounds"):
        supervisor.resume("whatever", extra_rounds=extra)


def test_continue_refuses_a_live_run(supervisor: RunSupervisor) -> None:
    created = supervisor.create_run(task="still working", max_rounds=2)

    with pytest.raises(ValueError, match="active run"):
        supervisor.resume(created["id"], mode="continue")


def test_continue_refuses_a_run_that_is_not_terminal(supervisor: RunSupervisor) -> None:
    run_id = "20990101T000000Z_idle"
    run_dir = supervisor.runs_root / run_id
    _terminal_run(supervisor, run_dir, run_id)
    # A dead worker with no report and no recognised lifecycle state is not
    # something resume may guess about.
    supervisor._bus(run_id).write_status({"run_id": run_id, "status": "mystery", "alive": False})

    with pytest.raises(ValueError, match="not resumable"):
        supervisor.resume(run_id, mode="continue")


def test_continue_replays_the_first_result_for_a_repeated_idempotency_key(
    supervisor: RunSupervisor,
) -> None:
    run_id = "20990101T000000Z_idem"
    run_dir = supervisor.runs_root / run_id
    _terminal_run(supervisor, run_dir, run_id)

    first = supervisor.resume(run_id, mode="continue", idempotency_key="key-1")
    second = supervisor.resume(run_id, mode="continue", idempotency_key="key-1")

    assert second["idempotent"] is True
    assert second["owner"]["resume_epoch"] == first["owner"]["resume_epoch"] == 1


def test_a_resumed_run_can_be_stopped_again(supervisor: RunSupervisor) -> None:
    run_id = "20990101T000000Z_stopagain"
    run_dir = supervisor.runs_root / run_id
    _terminal_run(supervisor, run_dir, run_id)
    # The previous generation already delivered a stop, which used to make the
    # next generation's stop look idempotently satisfied.
    (run_dir / "control" / "commands.jsonl").write_text(
        json.dumps({
            "command_id": "lifecycle-stop",
            "kind": "stop",
            "payload": {"signal": "SIGTERM"},
        })
        + "\n",
        encoding="utf-8",
    )
    resumed = supervisor.resume(run_id, mode="continue")
    # The fake Popen keeps the resumed worker alive so a stop is deliverable.
    assert supervisor.status(run_id)["alive"] is True
    assert resumed["owner"]["resume_epoch"] == 1

    stopped = supervisor.stop(run_id)

    # Before the epoch existed, the previous generation's ``lifecycle-stop``
    # receipt made this look already-delivered and the worker kept running.
    assert stopped["command_id"] == "lifecycle-stop@1"
    assert not stopped.get("idempotent")


# --- approval extra rounds --------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [(None, 0), (0, 0), (-3, 0), (True, 0), (4, 4), ("6", 6), (" 6 ", 6), ("six", 0), (1.5, 0), (MAX_ROUNDS + 5, MAX_ROUNDS)],
)
def test_normalise_extra_rounds(value: Any, expected: int) -> None:
    assert _normalise_extra_rounds(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [(None, 0), (0, 0), (-1, 0), (True, 0), ("5", 5), ("x", 0), (MAX_ROUNDS + 1, MAX_ROUNDS)],
)
def test_manager_extra_rounds_validation(value: Any, expected: int) -> None:
    assert _extra_rounds(value) == expected


def _resolved_hook_decision(tmp_path: Path, *, extra_rounds: int | None, trigger_max: bool) -> dict[str, Any]:
    state = DashboardState(log_dir=tmp_path / "logs", control_enabled=True)
    hook = make_human_hook(state, poll_interval=0.01)

    async def scenario() -> dict[str, Any]:
        task = asyncio.create_task(
            hook({
                "outcome": "progress" if trigger_max else "completed",
                "reached_max": trigger_max,
                "round_index": 5,
                "rounds": [],
            })
        )
        for _ in range(500):
            pending = [item for item in state.list_approvals() if item.get("status") == "pending"]
            if pending:
                break
            await asyncio.sleep(0.01)
        else:  # pragma: no cover - the hook always raises a gate here
            task.cancel()
            raise AssertionError("no approval was raised")
        approval_id = pending[0]["approval_id"]
        assert state.resolve_approval(
            approval_id,
            action="continue",
            extra_rounds=extra_rounds,
        )
        return await asyncio.wait_for(task, timeout=5)

    return asyncio.run(scenario())


def test_the_operator_round_grant_reaches_the_manager(tmp_path: Path) -> None:
    decision = _resolved_hook_decision(tmp_path, extra_rounds=7, trigger_max=True)

    # Previously hardcoded to 0, which silently discarded the operator's choice.
    assert decision["extra_rounds"] == 7
    assert decision["action"] == "continue"


def test_an_omitted_round_grant_keeps_the_manager_default(tmp_path: Path) -> None:
    decision = _resolved_hook_decision(tmp_path, extra_rounds=None, trigger_max=True)

    assert decision["extra_rounds"] == 0


def test_budget_gates_advertise_the_round_input(tmp_path: Path) -> None:
    _resolved_hook_decision(tmp_path, extra_rounds=2, trigger_max=True)
    state = DashboardState(log_dir=tmp_path / "logs", control_enabled=True)

    approvals = state.list_approvals()

    assert approvals
    assert all(item.get("context", {}).get("trigger") == "max_rounds" for item in approvals)
    assert any(item.get("allow_extra_rounds") for item in approvals)


def test_a_gate_without_the_round_input_rejects_a_grant(tmp_path: Path) -> None:
    state = DashboardState(log_dir=tmp_path / "logs", control_enabled=True)
    approval = state.create_approval(title="Answer me", allow_extra_rounds=False)

    assert state.resolve_approval(approval.approval_id, action="continue", extra_rounds=3) is False
    assert state.resolve_approval(approval.approval_id, action="continue") is True


# --- CLI surface ------------------------------------------------------------


def test_resume_is_refused_outside_a_supervised_worker(capsys, tmp_path: Path) -> None:
    # --resume reopens an existing run directory.  Only the supervisor verifies
    # that the run is terminal and owns the reservation, so a standalone caller
    # must not be able to reattach to somebody else's run.
    exit_code = cli.main([
        "run",
        "--task=anything",
        f"--runs-root={tmp_path / 'runs'}",
        f"--workspace={tmp_path}",
        "--resume",
    ])

    assert exit_code == 2
    assert "only available to supervised workers" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists(), "the guard must reject before reserving a run"


def test_the_worker_command_carries_resume_only_when_asked(tmp_path: Path) -> None:
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path)
    common = {
        "run_id": "r1",
        "task": "@/tmp/task.md",
        "agent": "codex",
        "model": None,
        "role_configs": None,
        "workspace": str(tmp_path),
        "max_rounds": 3,
        "prompt_language": "en",
    }

    assert "--resume" not in supervisor._worker_command(**common)
    assert "--resume" in supervisor._worker_command(**common, resume=True)
