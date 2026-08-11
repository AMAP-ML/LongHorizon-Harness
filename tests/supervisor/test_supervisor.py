from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lh_harness.supervisor.service import RunSupervisor
from lh_harness.webapi.server import create_app


class FakeProcess:
    pid = 4242
    returncode = None

    def poll(self):
        return self.returncode


def test_supervisor_creates_owned_run_without_touching_manager(monkeypatch, tmp_path: Path) -> None:
    process = FakeProcess()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    root = tmp_path / "runs"
    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace")
    result = supervisor.create_run(task="inspect the project", max_rounds=2)

    assert result["id"]
    run_dir = root / result["id"]
    assert (run_dir / "control" / "owner.json").is_file()
    assert supervisor.can_control(result["id"])
    assert supervisor.status(result["id"])["alive"] is True
    assert "--supervised" in result["owner"]["command"]


def test_web_supervisor_exposes_create_and_lifecycle_routes(monkeypatch, tmp_path: Path) -> None:
    process = FakeProcess()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("lh_harness.supervisor.service.os.killpg", lambda *args, **kwargs: None)
    root = tmp_path / "runs"
    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace")
    client = TestClient(create_app(runs_root=root, supervisor=supervisor))

    meta = client.get("/api/meta").json()
    assert meta["capabilities"]["create_run"] is True
    created = client.post("/api/runs", json={"task": "check status", "max_rounds": 1})
    assert created.status_code == 200
    run_id = created.json()["run"]["id"]
    assert client.get(f"/api/runs/{run_id}/status").json()["managed"] is True
    stopped = client.post(f"/api/runs/{run_id}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["signal"] == "SIGTERM"
    process.returncode = -15
    snapshot = client.get(f"/api/runs/{run_id}/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.json()["run"]["status"] == "cancelled"
    assert snapshot.json()["controls"]["can_resume"] is True


def test_websocket_pushes_supervisor_lifecycle_without_role_events(monkeypatch, tmp_path: Path) -> None:
    process = FakeProcess()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("lh_harness.supervisor.service.os.killpg", lambda *args, **kwargs: None)
    root = tmp_path / "runs"
    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace")
    client = TestClient(create_app(runs_root=root, supervisor=supervisor))
    created = client.post("/api/runs", json={"task": "stream lifecycle"}).json()["run"]

    with client.websocket_connect(f"/api/runs/{created['id']}/stream?replay=0") as websocket:
        assert websocket.receive_json()["data"]["run"]["status"] == "starting"
        supervisor.stop(created["id"])
        stopping = websocket.receive_json()
        assert stopping["kind"] == "snapshot"
        assert stopping["data"]["run"]["status"] == "stopping"
        assert stopping["data"]["controls"]["can_abort"] is False

        process.returncode = -15
        cancelled = websocket.receive_json()
        assert cancelled["kind"] == "snapshot"
        assert cancelled["data"]["run"]["status"] == "cancelled"
        assert cancelled["data"]["controls"]["can_resume"] is True


def test_web_create_and_resume_forward_idempotency_key(monkeypatch, tmp_path: Path) -> None:
    process = FakeProcess()
    launches: list[object] = []

    def launch(*args, **kwargs):
        launches.append(True)
        return process

    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", launch)
    root = tmp_path / "runs"
    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace")
    client = TestClient(create_app(runs_root=root, supervisor=supervisor))
    first = client.post("/api/runs", headers={"Idempotency-Key": "api-create-1"}, json={"task": "check"})
    second = client.post("/api/runs", headers={"Idempotency-Key": "api-create-1"}, json={"task": "check"})
    assert first.status_code == second.status_code == 200
    assert first.json()["run"]["id"] == second.json()["run"]["id"]
    assert second.json()["run"]["idempotent"] is True
    assert len(launches) == 1

    run_id = first.json()["run"]["id"]
    process.returncode = 1
    # A report-less fake exit is reconciled as failed and can be retried.
    assert client.get(f"/api/runs/{run_id}/status").status_code == 200
    resumed = client.post(f"/api/runs/{run_id}/resume", headers={"Idempotency-Key": "api-resume-1"})
    resumed_again = client.post(f"/api/runs/{run_id}/resume", headers={"Idempotency-Key": "api-resume-1"})
    assert resumed.status_code == resumed_again.status_code == 200
    assert resumed.json()["run"]["id"] == resumed_again.json()["run"]["id"]
    assert resumed_again.json()["run"]["idempotent"] is True
    assert len(launches) == 2


def test_run_summary_and_snapshot_expose_launch_provenance(monkeypatch, tmp_path: Path) -> None:
    process = FakeProcess()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    root = tmp_path / "runs"
    workspace = tmp_path / "workspace"
    supervisor = RunSupervisor(root, workspace_root=workspace)
    client = TestClient(create_app(runs_root=root, supervisor=supervisor))

    created = client.post(
        "/api/runs",
        json={
            "task": "show provenance",
            "agent": "claude_code",
            "model": "claude-custom-test",
            "roles": {
                "manager": {"agent": "claude_code", "model": "claude-manager-test"},
                "executor": {"agent": "codex", "model": "gpt-executor-test"},
                "auditor": {"agent": "claude_code", "model": "claude-auditor-test"},
            },
            "workspace": str(workspace),
            "max_rounds": 7,
            "prompt_language": "zh",
        },
    )
    assert created.status_code == 200
    run_id = created.json()["run"]["id"]

    summary = next(item for item in client.get("/api/runs").json()["runs"] if item["id"] == run_id)
    snapshot_run = client.get(f"/api/runs/{run_id}/snapshot").json()["run"]
    expected = {
        "agent": "claude_code",
        "model": "claude-custom-test",
        "workspace": str(workspace.resolve()),
        "max_rounds": 7,
        "prompt_language": "zh",
    }
    assert {key: summary[key] for key in expected} == expected
    assert {key: snapshot_run[key] for key in expected} == expected
    assert snapshot_run["role_configs"] == {
        "manager": {"agent": "claude_code", "model": "claude-manager-test"},
        "executor": {"agent": "codex", "model": "gpt-executor-test"},
        "auditor": {"agent": "claude_code", "model": "claude-auditor-test"},
    }


def test_supervisor_shutdown_stops_owned_live_workers(monkeypatch, tmp_path: Path) -> None:
    process = FakeProcess()
    signals: list[object] = []

    def killpg(_pid: int, sig: object) -> None:
        signals.append(sig)
        process.returncode = -15

    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("lh_harness.supervisor.service.os.killpg", killpg)
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    created = supervisor.create_run(task="stop with the Web API")

    supervisor.shutdown(grace_seconds=0.1)

    status = supervisor.status(created["id"])
    assert signals
    assert status["status"] == "cancelled"
    assert status["alive"] is False


def test_attached_supervisor_is_scoped_to_its_worker(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "runs"
    worker = root / "attached"
    (worker / "logs" / "role_management").mkdir(parents=True)
    other = root / "other"
    (other / "logs" / "role_management").mkdir(parents=True)
    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace", attached_only=True)
    supervisor.attach_run(run_id="attached", pid=os.getpid(), task="attached task", workspace=tmp_path / "workspace")

    # Embedded runs generate their id after this process starts, so it is not
    # present in argv.  The hosting PID is nevertheless unambiguously alive.
    assert supervisor.can_control("attached") is True
    with pytest.raises(ValueError, match="cannot access another run"):
        supervisor.status("other")
    with pytest.raises(ValueError, match="cannot (?:access|control) another? run|cannot control this run"):
        supervisor.stop("other")
