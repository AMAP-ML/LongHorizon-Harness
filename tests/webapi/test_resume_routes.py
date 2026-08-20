"""HTTP surface for in-place resume and operator round grants."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.supervisor.service import RunSupervisor
from lh_harness.types import MAX_ROUNDS
from lh_harness.webapi.server import create_app


class FakeProcess:
    pid = 4242
    returncode = None

    def poll(self):
        return self.returncode


@pytest.fixture()
def client(monkeypatch, tmp_path: Path):
    process = FakeProcess()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *a, **k: process)
    # os.killpg does not exist on Windows; the supervisor signals through the
    # process-group helpers there, so patch both ends and tolerate the absence.
    monkeypatch.setattr("lh_harness.supervisor.service.os.killpg", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        "lh_harness.utils.process_group._windows_deliver", lambda *a, **k: None, raising=False
    )
    root = tmp_path / "runs"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor = RunSupervisor(root, workspace_root=workspace)
    app = create_app(runs_root=root, supervisor=supervisor)
    return TestClient(app), supervisor, process


def _stopped_run(client: TestClient, process: FakeProcess) -> str:
    created = client.post("/api/runs", json={"task": "long job", "max_rounds": 4})
    assert created.status_code == 200
    run_id = created.json()["run"]["id"]
    assert client.post(f"/api/runs/{run_id}/stop", json={}).status_code == 200
    process.returncode = -15
    assert client.get(f"/api/runs/{run_id}/snapshot").json()["controls"]["can_resume"] is True
    return run_id


def test_resume_defaults_to_continuing_the_same_run(client) -> None:
    api, _supervisor, process = client
    run_id = _stopped_run(api, process)
    process.returncode = None

    response = api.post(f"/api/runs/{run_id}/resume", json={})

    assert response.status_code == 200
    run = response.json()["run"]
    assert run["id"] == run_id, "continue must reuse the run id"
    assert run["owner"]["resume_kind"] == "continue"
    assert run["owner"]["resume_epoch"] == 1
    # The owner projection must never leak argv/prompts to the browser.
    assert "command" not in run["owner"]


def test_resume_retry_creates_a_new_run(client) -> None:
    api, _supervisor, process = client
    run_id = _stopped_run(api, process)
    process.returncode = None

    response = api.post(f"/api/runs/{run_id}/resume", json={"mode": "retry"})

    assert response.status_code == 200
    assert response.json()["run"]["id"] != run_id
    assert response.json()["run"]["owner"]["resume_kind"] == "retry"


def test_resume_accepts_an_extra_round_grant(client) -> None:
    api, supervisor, process = client
    run_id = _stopped_run(api, process)
    process.returncode = None

    response = api.post(f"/api/runs/{run_id}/resume", json={"extra_rounds": 9})

    assert response.status_code == 200
    assert "--max-rounds=9" in supervisor.owner(run_id)["command"]


@pytest.mark.parametrize("mode", ["rewind", "CONTINUE", "continue retry", 7])
def test_resume_rejects_an_unknown_mode(client, mode: Any) -> None:
    api, _supervisor, process = client
    run_id = _stopped_run(api, process)

    response = api.post(f"/api/runs/{run_id}/resume", json={"mode": mode})

    assert response.status_code == 422


@pytest.mark.parametrize("body", [{}, {"mode": ""}])
def test_resume_treats_an_absent_mode_as_continue(client, body: dict[str, Any]) -> None:
    api, _supervisor, process = client
    run_id = _stopped_run(api, process)
    process.returncode = None

    response = api.post(f"/api/runs/{run_id}/resume", json=body)

    assert response.status_code == 200
    assert response.json()["run"]["id"] == run_id


@pytest.mark.parametrize("extra", [0, -1, MAX_ROUNDS + 1, 1.5, True, "many"])
def test_resume_rejects_an_out_of_range_grant(client, extra: Any) -> None:
    api, _supervisor, process = client
    run_id = _stopped_run(api, process)

    response = api.post(f"/api/runs/{run_id}/resume", json={"extra_rounds": extra})

    assert response.status_code == 422
    assert "extra_rounds" in response.json()["detail"]


def test_resume_replays_a_repeated_idempotency_key(client) -> None:
    api, _supervisor, process = client
    run_id = _stopped_run(api, process)
    process.returncode = None
    headers = {"Idempotency-Key": "resume-once"}

    first = api.post(f"/api/runs/{run_id}/resume", json={}, headers=headers)
    second = api.post(f"/api/runs/{run_id}/resume", json={}, headers=headers)

    assert first.status_code == second.status_code == 200
    assert second.json()["run"]["owner"]["resume_epoch"] == 1, "a replay must not bump the epoch"


def test_resume_conflicts_when_a_key_is_reused_for_another_request(client) -> None:
    api, _supervisor, process = client
    run_id = _stopped_run(api, process)
    process.returncode = None
    headers = {"Idempotency-Key": "shared"}

    assert api.post(f"/api/runs/{run_id}/resume", json={}, headers=headers).status_code == 200
    conflict = api.post(
        f"/api/runs/{run_id}/resume",
        json={"extra_rounds": 3},
        headers=headers,
    )

    assert conflict.status_code == 409


def test_a_resumed_run_can_be_stopped_again_over_http(client) -> None:
    api, _supervisor, process = client
    run_id = _stopped_run(api, process)
    process.returncode = None

    assert api.post(f"/api/runs/{run_id}/resume", json={}).status_code == 200
    stopped = api.post(f"/api/runs/{run_id}/stop", json={})

    assert stopped.status_code == 200
    assert stopped.json()["command_id"] == "lifecycle-stop@1"
    assert not stopped.json().get("idempotent"), "a new generation's stop is a real stop"


def test_the_snapshot_reports_a_stop_that_has_not_taken_effect(client) -> None:
    # SIGKILL costs the run its report, so Web only offers the escalation once a
    # graceful stop is provably in flight. That needs both fields.
    api, _supervisor, process = client
    created = api.post("/api/runs", json={"task": "ignores sigterm", "max_rounds": 2})
    run_id = created.json()["run"]["id"]

    idle = api.get(f"/api/runs/{run_id}/snapshot").json()["run"]
    assert "requested_action" not in idle
    assert "stop_requested_at" not in idle

    assert api.post(f"/api/runs/{run_id}/stop", json={}).status_code == 200
    stopping = api.get(f"/api/runs/{run_id}/snapshot").json()["run"]

    assert stopping["status"] == "stopping"
    assert stopping["requested_action"] == "stop"
    assert isinstance(stopping["stop_requested_at"], (int, float))

    assert api.post(f"/api/runs/{run_id}/abort", json={}).status_code == 200
    escalated = api.get(f"/api/runs/{run_id}/snapshot").json()["run"]

    assert escalated["requested_action"] == "abort", "an escalation must be observable"


def test_the_snapshot_exposes_the_resume_generation(client) -> None:
    # Clients keep lifecycle monotonic, so without this counter a reopened run
    # looks like a stale non-terminal frame after a terminal one and is dropped:
    # the UI kept showing the ended run until the page was reloaded.
    api, _supervisor, process = client
    run_id = _stopped_run(api, process)

    assert "resume_epoch" not in api.get(f"/api/runs/{run_id}/snapshot").json()["run"]

    process.returncode = None
    assert api.post(f"/api/runs/{run_id}/resume", json={}).status_code == 200
    snapshot = api.get(f"/api/runs/{run_id}/snapshot").json()

    assert snapshot["run"]["resume_epoch"] == 1
    assert snapshot["run"]["status"] not in {"cancelled", "failed", "completed", "incomplete", "blocked"}


def test_approval_resolve_rejects_an_out_of_range_round_grant(client, tmp_path: Path) -> None:
    api, _supervisor, process = client
    created = api.post("/api/runs", json={"task": "gate me", "max_rounds": 2})
    run_id = created.json()["run"]["id"]

    response = api.post(
        f"/api/runs/{run_id}/approvals/does-not-matter/resolve",
        json={"action": "continue", "extra_rounds": MAX_ROUNDS + 1},
    )

    assert response.status_code == 422
    assert "extra_rounds" in response.json()["detail"]
