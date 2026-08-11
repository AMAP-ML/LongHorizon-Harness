from __future__ import annotations

import json
import os
import base64
import shutil
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import DashboardState
from lh_harness.webapi.events import EventTailer
from lh_harness.webapi.server import create_app


def _run(tmp_path: Path) -> tuple[Path, DashboardState]:
    root = tmp_path / "runs"
    run = root / "run-1"
    role = run / "logs" / "role_management"
    round_dir = role / "rounds" / "round_001"
    round_dir.mkdir(parents=True)
    (role / "events.jsonl").write_text(
        json.dumps({"event": "role_harness_start", "ts": 1}) + "\n",
        encoding="utf-8",
    )
    return root, DashboardState(run / "logs", runs_root=root, control_enabled=True)


def _ws_auth_protocols(token: str) -> list[str]:
    encoded = base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii").rstrip("=")
    return ["lh-harness-auth.v1", f"lh-harness-token.{encoded}"]


def test_event_tail_is_absolute_and_bad_records_are_diagnostic(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for index in range(250):
            handle.write(json.dumps({"event": "manager_round_done", "round": index + 1}) + "\n")
        handle.write(json.dumps({"schema_version": "bad", "event": "broken"}) + "\n")
    tailer = EventTailer(path, run_id="run-1")
    items = tailer.read(limit=200)
    assert items[0].event_id == "run-1:000051"
    assert items[-1].event_id == "run-1:000250"
    assert any("schema_version" in warning for warning in tailer.last_warnings)

    replay = tailer.read(after="run-1:000001", limit=500)
    assert replay[0].event_id == "run-1:000002"
    contiguous = tailer.read(after="run-1:000001", limit=2)
    assert [item.event_id for item in contiguous] == ["run-1:000002", "run-1:000003"]
    tailer.read(after="run-1:999999")
    assert tailer.last_cursor_gap is True
    assert tailer.last_resync_required is True


def test_event_tail_rejects_symlink_and_hardlink_aliases(tmp_path: Path) -> None:
    outside = tmp_path / "outside-events.jsonl"
    outside.write_text(json.dumps({"event": "secret"}) + "\n", encoding="utf-8")
    link = tmp_path / "events.jsonl"
    link.symlink_to(outside)
    assert EventTailer(link, run_id="run-1").read() == []

    link.unlink()
    try:
        link.hardlink_to(outside)
    except OSError:
        pytest.skip("filesystem does not support hard links")
    assert EventTailer(link, run_id="run-1").read() == []


def test_event_tail_rejects_foreign_run_identity_and_duplicate_cursors(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    records = [
        {"event_id": "run-1:000001", "run_id": "run-1", "event": "role_harness_start"},
        # Neither an explicit foreign run id nor a foreign cursor may cross
        # the stream boundary selected by the API route.
        {"event_id": "run-2:000002", "run_id": "run-2", "event": "role_harness_done"},
        {"event_id": "run-2:000003", "event": "role_harness_done"},
        # Duplicate ids make replay ambiguous.  The earliest record wins.
        {"event_id": "run-1:000001", "event": "role_harness_done"},
        {"event_id": "run-1:000005", "event": "manager_round_done", "round": 1},
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")

    tailer = EventTailer(path, run_id="run-1")
    items = tailer.read(limit=20)

    assert [item.event_id for item in items] == ["run-1:000001", "run-1:000005"]
    assert all(item.run_id == "run-1" for item in items)
    assert any("run_id does not match" in warning for warning in tailer.last_warnings)
    assert any("does not belong" in warning for warning in tailer.last_warnings)
    assert any("duplicate event_id" in warning for warning in tailer.last_warnings)


def test_event_tail_is_byte_bounded_and_uses_stable_offset_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import lh_harness.webapi.events as events_module

    monkeypatch.setattr(events_module, "_MAX_EVENT_LOG_BYTES", 180)
    path = tmp_path / "events.jsonl"
    path.write_text(
        "".join(
            json.dumps(
                {
                    "schema_version": 1,
                    "event_id": f"run-1:{index:06d}",
                    "event": "manager_round_done",
                    "round": index,
                }
            )
            + "\n"
            for index in range(1, 20)
        )
        # A legacy record without an id exercises the offset-based fallback
        # used once the head of the file is outside the retention window.
        + json.dumps({"event": "manager_round_start", "round": 20})
        + "\n",
        encoding="utf-8",
    )

    tailer = events_module.EventTailer(path, run_id="run-1")
    items = tailer.read(limit=20)

    assert items
    assert all(item.offset is not None for item in items)
    assert any(item.event_id.startswith("run-1:offset-") for item in items)
    assert any("exceeds" in warning for warning in tailer.last_warnings)

    # A cursor from the discarded head must force a snapshot resync, not a
    # full-file read or an apparently complete replay.
    tailer.read(after="run-1:000001")
    assert tailer.last_resync_required is True


def test_api_auth_and_websocket_origin_are_enforced(tmp_path: Path) -> None:
    root, state = _run(tmp_path)
    app = create_app(state=state, runs_root=root, run_id="run-1", auth_token="secret")
    client = TestClient(app)
    assert client.get("/api/meta").status_code == 401
    assert client.get("/api/meta", headers={"Authorization": "Bearer secret"}).status_code == 200
    with pytest.raises(Exception):
        with client.websocket_connect(
            "/api/runs/run-1/stream?replay=0&token=secret",
            headers={"Origin": "https://evil.example"},
        ):
            pass
    # The long-lived token must not be accepted from a query string.  The
    # Browser transport uses a bounded base64url subprotocol instead.
    with pytest.raises(Exception):
        with client.websocket_connect("/api/runs/run-1/stream?replay=0&token=secret"):
            pass
    with client.websocket_connect(
        "/api/runs/run-1/stream?replay=0",
        subprotocols=_ws_auth_protocols("secret"),
    ) as websocket:
        assert websocket.receive_json()["kind"] == "snapshot"
        assert websocket.accepted_subprotocol == "lh-harness-auth.v1"
        websocket.close()


def test_multirun_registry_does_not_create_phantom_local_run(tmp_path: Path) -> None:
    root, state = _run(tmp_path)
    client = TestClient(create_app(state=state, runs_root=root))
    assert client.get("/api/runs/local/snapshot").status_code == 404
    assert not (root / "local" / "control").exists()


def test_html_artifacts_are_attachments(tmp_path: Path) -> None:
    root, state = _run(tmp_path)
    target = root / "run-1" / "logs" / "role_management" / "rounds" / "round_001" / "proof.html"
    target.write_text("<script>alert(1)</script>", encoding="utf-8")
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))
    response = client.get("/api/runs/run-1/rounds/1/artifacts/proof.html/raw")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["content-disposition"].startswith("attachment;")


def test_non_raster_documents_are_attachments_and_filename_is_header_safe(tmp_path: Path) -> None:
    root, state = _run(tmp_path)
    round_dir = root / "run-1" / "logs" / "role_management" / "rounds" / "round_001"
    # The filesystem permits both quote-bearing and non-ASCII names.  They
    # must not make Starlette fail to encode the response header or alter its
    # disposition semantics.
    quoted = round_dir / 'x";foo.pdf'
    quoted.write_bytes(b"%PDF-fixture")
    unicode_name = round_dir / "你好.svgz"
    unicode_name.write_bytes(b"not-an-inline-document")
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    pdf = client.get('/api/runs/run-1/rounds/1/artifacts/x%22%3Bfoo.pdf/raw')
    svgz = client.get('/api/runs/run-1/rounds/1/artifacts/%E4%BD%A0%E5%A5%BD.svgz/raw')

    assert pdf.status_code == 200
    assert pdf.headers["content-type"].startswith("application/octet-stream")
    assert pdf.headers["content-disposition"].startswith('attachment; filename="x_foo.pdf"; filename*=UTF-8\'\'')
    assert svgz.status_code == 200
    assert svgz.headers["content-type"].startswith("text/plain")
    assert svgz.headers["content-disposition"].startswith('attachment; filename="artifact.svgz"; filename*=UTF-8\'\'')


def test_artifact_query_token_is_not_an_authentication_fallback(tmp_path: Path) -> None:
    root, state = _run(tmp_path)
    target = root / "run-1" / "logs" / "role_management" / "rounds" / "round_001" / "proof.txt"
    target.write_text("private", encoding="utf-8")
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1", auth_token="secret"))

    response = client.get("/api/runs/run-1/rounds/1/artifacts/proof.txt/raw?token=secret")

    assert response.status_code == 401


def test_artifact_round_symlink_cannot_escape_log_root(tmp_path: Path) -> None:
    root, state = _run(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not for the dashboard", encoding="utf-8")
    rounds = root / "run-1" / "logs" / "role_management" / "rounds"
    real_round = rounds / "round_001"
    real_round.rmdir()
    real_round.symlink_to(outside, target_is_directory=True)
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    listed = client.get("/api/runs/run-1/rounds/1/artifacts")
    assert listed.status_code == 200
    assert listed.json()["artifacts"] == []
    assert client.get("/api/runs/run-1/rounds/1/artifacts/secret.txt").status_code == 404


def test_run_symlink_cannot_escape_runs_root_or_cached_state(tmp_path: Path) -> None:
    root, state = _run(tmp_path)
    outside = tmp_path / "outside"
    (outside / "logs" / "role_management").mkdir(parents=True)
    (outside / "logs" / "report.json").write_text(
        json.dumps({"task": "SECRET_OUTSIDE", "status": "complete"}), encoding="utf-8"
    )
    (root / "evil").symlink_to(outside, target_is_directory=True)

    client = TestClient(create_app(state=state, runs_root=root))
    assert all(item["id"] != "evil" for item in client.get("/api/runs").json()["runs"])
    assert client.get("/api/runs/evil/snapshot").status_code == 404
    assert client.get("/api/runs/run-1/snapshot").status_code == 200
    assert "SECRET_OUTSIDE" not in client.get("/api/runs/run-1/snapshot").text


@pytest.mark.parametrize("target_inside_root", [True, False])
def test_logs_symlink_cannot_cross_run_for_scan_select_or_registry(
    tmp_path: Path,
    target_inside_root: bool,
) -> None:
    """A logs link must not turn run-1 into run-2 (or an external run)."""

    root, state = _run(tmp_path)
    sibling_logs = root / "run-2" / "logs" / "role_management"
    sibling_logs.mkdir(parents=True)
    (sibling_logs / "report.json").write_text(
        json.dumps({"task": "SECRET_SIBLING", "status": "complete"}),
        encoding="utf-8",
    )
    outside_logs = tmp_path / "outside" / "logs" / "role_management"
    outside_logs.mkdir(parents=True)
    (outside_logs / "report.json").write_text(
        json.dumps({"task": "SECRET_EXTERNAL", "status": "complete"}),
        encoding="utf-8",
    )
    target = sibling_logs.parent if target_inside_root else outside_logs.parent
    original_logs = root / "run-1" / "logs"
    shutil.rmtree(original_logs)
    original_logs.symlink_to(target, target_is_directory=True)

    # The state was cached before the swap.  Registry validation must still
    # reject it rather than trusting the old DashboardState object.
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))
    assert client.get("/api/runs/run-1/snapshot").status_code == 404
    listed = client.get("/api/runs").json()["runs"]
    assert all(item["id"] != "run-1" for item in listed)
    assert "SECRET_" not in client.get("/api/runs/run-1/snapshot").text

    browser_state = DashboardState(runs_root=root)
    assert browser_state.select_run("run-1") is False


def test_artifact_and_trajectory_final_symlinks_are_not_followed(tmp_path: Path) -> None:
    root, state = _run(tmp_path)
    round_dir = root / "run-1" / "logs" / "role_management" / "rounds" / "round_001"
    outside = tmp_path / "outside-files"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("must stay private", encoding="utf-8")
    (round_dir / "secret.txt").symlink_to(secret)
    (round_dir / "manager_raw_trajectory.jsonl").symlink_to(secret)

    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))
    assert client.get("/api/runs/run-1/rounds/1/artifacts/secret.txt").status_code == 404
    assert client.get("/api/runs/run-1/rounds/1/artifacts/secret.txt/raw").status_code == 404
    assert client.get("/api/runs/run-1/rounds/1/trajectory/manager").status_code == 404


@pytest.mark.parametrize("child", ["role_management", "rounds"])
def test_run_registry_rejects_nested_role_boundary_symlinks(tmp_path: Path, child: str) -> None:
    root, state = _run(tmp_path)
    role = root / "run-1" / "logs" / "role_management"
    if child == "role_management":
        real = tmp_path / "real-role"
        real.mkdir()
        shutil.rmtree(role)
        role.symlink_to(real, target_is_directory=True)
    else:
        rounds = role / "rounds"
        real = tmp_path / "real-rounds"
        real.mkdir()
        shutil.rmtree(rounds)
        rounds.symlink_to(real, target_is_directory=True)
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))
    assert client.get("/api/runs/run-1/snapshot").status_code == 404
    assert all(item["id"] != "run-1" for item in client.get("/api/runs").json()["runs"])
    assert DashboardState(runs_root=root).select_run("run-1") is False


def test_artifact_read_rechecks_with_no_follow_fd_after_path_validation(tmp_path: Path) -> None:
    root, state = _run(tmp_path)
    round_dir = root / "run-1" / "logs" / "role_management" / "rounds" / "round_001"
    target = round_dir / "race.txt"
    target.write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside-race.txt"
    outside.write_text("private", encoding="utf-8")

    class SwappingState(DashboardState):
        def resolve_round_artifact(self, round_index: int, name: str):  # type: ignore[override]
            resolved = super().resolve_round_artifact(round_index, name)
            if resolved is not None and resolved == target:
                target.unlink()
                target.symlink_to(outside)
            return resolved

    swapping = SwappingState(state.log_dir, runs_root=root, control_enabled=False)
    assert swapping.read_round_artifact(1, "race.txt") is None


def test_oversized_state_files_are_bounded(tmp_path: Path) -> None:
    root, state = _run(tmp_path)
    round_dir = root / "run-1" / "logs" / "role_management" / "rounds" / "round_001"
    # Keep the fixture independent of the production constants while still
    # exercising the same bounded reader through a deliberately large file.
    (round_dir / "executor_output.txt").write_text("x" * (600 * 1024), encoding="utf-8")
    (round_dir / "executor_raw_trajectory.jsonl").write_text("x" * (17 * 1024 * 1024), encoding="utf-8")

    snapshot = state.snapshot()
    round_snapshot = snapshot["rounds"][0]
    assert "truncated" in round_snapshot["executor_output"]
    trajectory = state.read_trajectory(1, "executor")
    assert trajectory is not None
    assert trajectory["warning"] == "trajectory is too large to render"


def test_trajectory_step_count_is_bounded_and_reports_latest_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dense but valid JSONL stream must not produce an unbounded UI payload."""

    import lh_harness.dashboard.state as state_module

    monkeypatch.setattr(state_module, "_MAX_TRAJECTORY_STEPS", 3)
    root, state = _run(tmp_path)
    trajectory_path = (
        root
        / "run-1"
        / "logs"
        / "role_management"
        / "rounds"
        / "round_001"
        / "manager_raw_trajectory.jsonl"
    )
    trajectory_path.write_text(
        "".join(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": f"message-{index}",
                        "type": "agent_message",
                        "text": f"step {index}",
                    },
                }
            )
            + "\n"
            for index in range(1, 7)
        ),
        encoding="utf-8",
    )

    trajectory = state.read_trajectory(1, "manager")

    assert trajectory is not None
    assert trajectory["step_count"] == 3
    assert len(trajectory["steps"]) == 3
    assert [step["text"] for step in trajectory["steps"]] == ["step 4", "step 5", "step 6"]
    assert trajectory["steps_truncated"] is True
    assert "latest 3" in trajectory["warning"]


def test_small_trajectory_has_no_truncation_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import lh_harness.dashboard.state as state_module

    monkeypatch.setattr(state_module, "_MAX_TRAJECTORY_STEPS", 3)
    root, state = _run(tmp_path)
    trajectory_path = (
        root
        / "run-1"
        / "logs"
        / "role_management"
        / "rounds"
        / "round_001"
        / "manager_raw_trajectory.jsonl"
    )
    trajectory_path.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "message-1", "type": "agent_message", "text": "only step"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    trajectory = state.read_trajectory(1, "manager")

    assert trajectory is not None
    assert trajectory["step_count"] == 1
    assert "warning" not in trajectory
    assert "steps_truncated" not in trajectory


def test_jsonl_state_reader_retains_bounded_complete_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import lh_harness.dashboard.state as state_module

    monkeypatch.setattr(state_module, "_MAX_JSONL_BYTES", 160)
    path = tmp_path / "records.jsonl"
    path.write_text(
        "".join(json.dumps({"index": index, "value": "x" * 20}) + "\n" for index in range(20)),
        encoding="utf-8",
    )
    records = state_module._read_jsonl(path)
    assert records
    assert records[-1]["index"] == 19
    assert records[0]["index"] > 0

    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps({"secret": True}) + "\n", encoding="utf-8")
    link = tmp_path / "link.jsonl"
    link.symlink_to(outside)
    assert state_module._read_jsonl(link) == []


def test_create_run_rejects_float_rounds(tmp_path: Path) -> None:
    # No supervisor is needed to validate the request shape; the endpoint must
    # reject malformed input before attempting a process launch.
    root, state = _run(tmp_path)
    from lh_harness.supervisor.service import RunSupervisor

    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace")
    client = TestClient(create_app(state=state, runs_root=root, supervisor=supervisor))
    response = client.post("/api/runs", json={"task": "x", "max_rounds": 1.5})
    assert response.status_code == 422


def test_control_revision_rejects_lossy_numeric_forms(tmp_path: Path) -> None:
    root, state = _run(tmp_path)
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))
    for value in (1.5, True, "1.5", "1e0", ""):
        response = client.post(
            "/api/runs/run-1/instructions",
            json={"instructions": "note", "expected_revision": value},
        )
        assert response.status_code == 422, value


def test_create_run_enforces_round_ceiling(tmp_path: Path) -> None:
    root, state = _run(tmp_path)
    from lh_harness.supervisor.service import RunSupervisor
    from lh_harness.types import MAX_ROUNDS

    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace")
    client = TestClient(create_app(state=state, runs_root=root, supervisor=supervisor))
    response = client.post("/api/runs", json={"task": "x", "max_rounds": MAX_ROUNDS + 1})
    assert response.status_code == 422


def test_websocket_resync_gap_advances_to_retained_tail(tmp_path: Path) -> None:
    """A truncated log should resync once and continue streaming new events."""

    root, state = _run(tmp_path)
    events_path = root / "run-1" / "logs" / "role_management" / "events.jsonl"
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    # Starting from the known first event makes the cursor deterministic and
    # avoids relying on the timing of the initial replay read.
    with client.websocket_connect(
        "/api/runs/run-1/stream?replay=0&after=run-1%3A000001"
    ) as websocket:
        assert websocket.receive_json()["kind"] == "snapshot"
        time.sleep(0.05)
        events_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "event_id": "run-1:000003",
                    "event": "manager_round_done",
                    "round_index": 1,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        assert websocket.receive_json()["kind"] == "resync_required"
        assert websocket.receive_json()["kind"] == "snapshot"

        with events_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_id": "run-1:000004",
                        "event": "role_harness_done",
                    }
                )
                + "\n"
            )

        observed = []
        for _ in range(8):
            message = websocket.receive_json()
            if message.get("kind") == "event":
                observed.append(message["data"].get("event_id"))
                if "run-1:000004" in observed:
                    break
        assert "run-1:000004" in observed


def test_attached_api_rejects_foreign_run_paths(tmp_path: Path) -> None:
    root, state = _run(tmp_path)
    from lh_harness.supervisor.service import RunSupervisor

    other = root / "run-2" / "logs" / "role_management"
    other.mkdir(parents=True)
    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace", attached_only=True)
    supervisor.attach_run(run_id="run-1", pid=os.getpid(), workspace=tmp_path / "workspace")
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1", supervisor=supervisor))

    assert client.get("/api/runs/run-2/snapshot").status_code == 404
    assert client.post("/api/runs/run-2/stop").status_code == 404
