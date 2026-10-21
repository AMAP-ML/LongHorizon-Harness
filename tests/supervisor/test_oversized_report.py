"""A report that outgrows the control-record cap must still be read.

A finished report embeds every round, so a long run can legally exceed the
8 MiB cap that the supervisor's generic control-record reader enforces.  The
lifecycle pollers treat an empty dict as *no report*, and the failure path
then overwrites the real report with a synthetic stub, so a report that only
looked missing must be read in full instead of silently dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import lh_harness.supervisor.service as service
from lh_harness.supervisor.service import RunSupervisor

# 10 MiB comfortably beats the 8 MiB cap but stays a fast test fixture.
_BLOB_SIZE = 10 * 1024 * 1024


class _ExitedProcess:
    pid = 7777
    returncode = None

    def poll(self) -> int | None:
        return self.returncode


def _started_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[RunSupervisor, str, Path, _ExitedProcess]:
    process = _ExitedProcess()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *a, **k: process)
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    created = supervisor.create_run(task="long run")
    run_dir = tmp_path / "runs" / created["id"]
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    return supervisor, created["id"], run_dir / "logs", process


def _write_report(logs: Path, report: dict[str, object]) -> None:
    (logs / "report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


def _completed_report(extra_bytes: int = 0) -> dict[str, object]:
    return {
        "schema_version": 2,
        "status": "complete",
        "completion_satisfied": True,
        "rounds_run": 90,
        "final_response": "done",
        # Rounds carry full executor transcripts; this stands in for them.
        "rounds": [{"round_index": 1, "plan_text": "x" * extra_bytes}],
    }


def test_oversized_completed_report_is_not_discarded_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, run_id, logs, process = _started_run(tmp_path, monkeypatch)
    _write_report(logs, _completed_report(_BLOB_SIZE))
    process.returncode = 0

    status = supervisor.status(run_id)

    assert status["status"] == "completed"
    assert status["report_status"] == "completed"
    assert "failure_reason" not in status
    assert not (logs / "crash_report.json").exists()
    # The real report must survive untouched: the stub overwrite is the bug.
    persisted = json.loads((logs / "report.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "complete"
    assert persisted["completion_satisfied"] is True
    assert len(persisted["rounds"][0]["plan_text"]) == _BLOB_SIZE


def test_oversized_failure_report_is_still_the_failure_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, run_id, logs, process = _started_run(tmp_path, monkeypatch)
    report = {
        "schema_version": 2,
        "status": "failed",
        "completion_satisfied": False,
        "failure_reason": "the executor backend timed out",
        "rounds": [{"round_index": 1, "plan_text": "x" * _BLOB_SIZE}],
    }
    _write_report(logs, report)
    process.returncode = 1

    status = supervisor.status(run_id)

    assert status["status"] == "failed"
    assert status["report_status"] == "failed"
    # The report's own reason must win, not the generic exit-code fallback.
    assert status["failure_reason"] == "the executor backend timed out"
    persisted = json.loads((logs / "report.json").read_text(encoding="utf-8"))
    assert persisted == report


def test_an_oversized_report_far_beyond_the_cap_is_still_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, run_id, logs, process = _started_run(tmp_path, monkeypatch)
    # Sparse: the cap check happens before any read, so one byte on disk is
    # enough to stand in for a multi-gigabyte hostile file.
    with (logs / "report.json").open("wb") as handle:
        handle.seek(256 * 1024 * 1024 + 4096)
        handle.write(b"\x00")
    process.returncode = 0

    status = supervisor.status(run_id)

    assert status["status"] == "failed"
    assert (logs / "crash_report.json").exists()


def test_read_report_parses_beyond_the_control_cap(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    report = _completed_report(9 * 1024 * 1024)
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    assert service._read_report(path)["completion_satisfied"] is True
    assert (
        service._read_report(path)["rounds"][0]["plan_text"]
        == "x" * (9 * 1024 * 1024)
    )
    assert service._read_report(tmp_path / "missing.json") == {}


def test_read_report_rejects_files_beyond_the_hard_ceiling(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    with path.open("wb") as handle:
        handle.seek(256 * 1024 * 1024 + 1)
        handle.write(b"\x00")

    assert service._read_report(path) == {}