from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.agent_logs import parse_trajectory
from lh_harness.dashboard.state import DashboardState
from lh_harness.supervisor.control_bus import ControlBus
from lh_harness.webapi.events import EventTailer, normalize_event
from lh_harness.webapi.server import create_app
from lh_harness.webapi.snapshot import build_snapshot


def _fixture(tmp_path: Path, *, control_enabled: bool = True) -> tuple[Path, DashboardState]:
    root = tmp_path / "runs"
    run = root / "run-1"
    role_dir = run / "logs" / "role_management"
    round_dir = role_dir / "rounds" / "round_001"
    round_dir.mkdir(parents=True)
    (run / "logs" / "report.json").write_text(json.dumps({"task": "fixture"}), encoding="utf-8")
    (role_dir / "events.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": 1, "event": "role_harness_start"}),
                json.dumps({"ts": 2, "event": "manager_round_start", "round_index": 1}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (round_dir / "manager_plan.txt").write_text("next_step: execute", encoding="utf-8")
    (round_dir / "task_state.txt").write_text("fixture state", encoding="utf-8")
    (round_dir / "screenshot.png").write_bytes(b"\x89PNG\r\nfixture-image")
    return root, DashboardState(run / "logs", runs_root=root, control_enabled=control_enabled)


def test_event_normalization_and_partial_tail(tmp_path: Path) -> None:
    event = normalize_event(
        {"ts": 1, "event": "manager_round_start", "round_index": 3},
        run_id="run-1",
        sequence=7,
    )
    assert event.event_id == "run-1:000007"
    assert event.type == "round.manager.started"
    assert event.round == 3
    assert event.status == "running"

    reply = normalize_event(
        {"ts": 2, "event": "final_response_done", "round": 3},
        run_id="run-1",
        sequence=8,
    )
    assert reply.type == "round.final_response.completed"
    assert reply.role == "final_response"
    assert reply.status == "completed"

    legacy_episode_status = {
        "status": "done",
        "duration_ms": 42,
        "exit_code": 0,
    }
    legacy_done = normalize_event(
        {
            "ts": 3,
            "event": "manager_round_done",
            "round": 3,
            "status": legacy_episode_status,
        },
        run_id="run-1",
        sequence=9,
    )
    assert legacy_done.status == "completed"
    assert legacy_done.payload["episode_status"] == legacy_episode_status

    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps({"ts": 1, "event": "role_harness_start"}) + "\n"
        + json.dumps({"ts": 2, "event": "manager_round_start"}),
        encoding="utf-8",
    )
    tailer = EventTailer(path, run_id="run-1")
    items = tailer.read()
    assert [item.event_id for item in items] == ["run-1:000001"]
    path.open("a", encoding="utf-8").write("\n")
    assert [item.event_id for item in tailer.read()] == ["run-1:000001", "run-1:000002"]


def test_trajectory_preserves_inline_screenshot() -> None:
    raw = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "shot-1",
                "type": "mcp_tool_call",
                "result": {
                    "content": [
                        {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
                    ],
                },
            },
        }
    )

    steps = parse_trajectory(raw)

    assert steps == [
        {
            "kind": "tool_use",
            "id": "shot-1",
            "name": "mcp",
            "input": {"arguments": None},
        },
        {
            "kind": "tool_result",
            "tool_use_id": "shot-1",
            "text": "[image]",
            "images": ["data:image/png;base64,aGVsbG8="],
            "has_image": True,
            "is_error": False,
        },
    ]


def test_api_resolves_normalized_screenshot_file_reference(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path)
    round_dir = root / "run-1" / "logs" / "role_management" / "rounds" / "round_001"
    (round_dir / "executor_raw_trajectory.jsonl").write_text("{}\n", encoding="utf-8")
    (round_dir / "executor_step_0001_01.png").write_bytes(b"\x89PNG\r\nnormalized")
    (round_dir / "executor_trajectory.jsonl").write_text(
        json.dumps(
            {
                "step_num": 1,
                "kind": "tool_result",
                "text": "[image]",
                "has_image": True,
                "screenshot_file": "executor_step_0001_01.png",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    response = client.get("/api/runs/run-1/rounds/1/trajectory/executor")

    assert response.status_code == 200
    body = response.json()
    assert body["trajectory_source"] == "normalized"
    assert body["steps"][0]["images"] == [
        "/api/runs/run-1/rounds/1/artifacts/executor_step_0001_01.png/raw"
    ]
    assert "data:image" not in response.text


def test_codex_search_and_todo_completion_is_not_left_running() -> None:
    raw = "\n".join(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": item_id,
                    "type": item_type,
                    "status": "completed",
                    "query": "find release notes" if item_type == "web_search" else None,
                },
            }
        )
        for item_id, item_type in (("search-1", "web_search"), ("todo-1", "todo_list"))
    )
    steps = parse_trajectory(raw)
    results = [item for item in steps if item.get("kind") == "tool_result"]
    assert [item.get("tool_use_id") for item in results] == ["search-1", "todo-1"]
    assert all(item.get("status") == "completed" for item in results)


def test_completed_snapshot_clears_active_round_and_exposes_completion_evidence(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run = root / "run-1"
    role_dir = run / "logs" / "role_management"
    round_dir = role_dir / "rounds" / "round_001"
    round_dir.mkdir(parents=True)
    report = {
        "status": "complete",
        "task": "fixture complete",
        "completion_satisfied": True,
        "completion_authority": "manager_with_role_auditors",
        "exit_code": 0,
        "final_response": "The requested change is complete.",
    }
    (run / "logs" / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (role_dir / "events.jsonl").write_text(
        json.dumps({"event": "manager_round_start", "round_index": 1, "active_role": "manager"}) + "\n",
        encoding="utf-8",
    )
    (round_dir / "manager_plan.txt").write_text("next_step: done", encoding="utf-8")
    (round_dir / "final_response.txt").write_text(
        "The requested change is complete.", encoding="utf-8"
    )
    state = DashboardState(run / "logs", runs_root=root, control_enabled=False)

    snapshot = build_snapshot(state, run_id="run-1")
    assert snapshot["run"]["status"] == "completed"
    assert snapshot["run"]["completion_satisfied"] is True
    assert snapshot["run"]["completion_authority"] == "manager_with_role_auditors"
    assert snapshot["run"]["exit_code"] == 0
    assert snapshot["run"]["final_response"] == "The requested change is complete."
    assert snapshot["rounds"][0]["final_response"] == "The requested change is complete."
    assert snapshot["active_round"] is None
    assert snapshot["active_role"] is None


def test_active_snapshot_uses_supervisor_owner_task_before_first_report(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run = root / "run-1"
    role_dir = run / "logs" / "role_management"
    role_dir.mkdir(parents=True)
    (role_dir / "events.jsonl").write_text(
        json.dumps({"event": "role_harness_start"}) + "\n",
        encoding="utf-8",
    )
    ControlBus(run).write_owner({"run_id": "run-1", "task": "owner task while active"})
    state = DashboardState(run / "logs", runs_root=root, control_enabled=True)

    snapshot = build_snapshot(state, run_id="run-1")

    assert snapshot["run"]["status"] == "running"
    assert snapshot["mission"]["task"] == "owner task while active"


def test_svg_artifacts_are_text_attachments_not_image_documents(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path)
    svg = root / "run-1" / "logs" / "role_management" / "rounds" / "round_001" / "proof.svg"
    svg.write_text("<svg><script>window.pwned=true</script></svg>", encoding="utf-8")
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    raw = client.get("/api/runs/run-1/rounds/1/artifacts/proof.svg/raw")
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("text/plain")
    assert raw.headers["content-disposition"].startswith("attachment;")


def test_legacy_static_dashboard_routes_are_not_registered(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path)
    app = create_app(state=state, runs_root=root, run_id="run-1")
    paths = {getattr(route, "path", "") for route in app.routes}
    page = TestClient(app).get("/")

    assert page.status_code == 200
    assert '<div id="root"></div>' in page.text
    assert "/api/state" not in paths
    assert "/api/round/{round_index}" not in paths
    assert "/api/round/{round_index}/{name}" not in paths
    assert "/api/inject" not in paths


def test_api_snapshot_replay_and_artifact(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path)
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    approval = state.create_approval(title="Continue fixture", message="Proceed?")

    meta = client.get("/api/meta")
    assert meta.status_code == 200
    assert meta.json()["protocol_version"] == "1"

    snapshot = client.get("/api/runs/run-1/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.json()["run"]["status"] == "waiting_approval"
    assert snapshot.json()["active_round"] == 1
    assert snapshot.json()["controls"] == {
        "can_inject": True,
        "can_abort": False,
        "can_resume": False,
    }

    events = client.get("/api/runs/run-1/events?limit=20").json()["events"]
    assert [item["type"] for item in events] == ["run.started", "round.manager.started"]
    replay = client.get(f"/api/runs/run-1/events?after={events[0]['event_id']}").json()["events"]
    assert [item["event_id"] for item in replay] == [events[1]["event_id"]]

    artifact = client.get("/api/runs/run-1/rounds/1/artifacts/manager_plan.txt")
    assert artifact.status_code == 200
    assert "execute" in artifact.text
    image = client.get("/api/runs/run-1/rounds/1/artifacts/screenshot.png/raw")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content == b"\x89PNG\r\nfixture-image"
    assert client.get("/api/runs/run-1/rounds/1/artifacts/../screenshot.png/raw").status_code in {404, 422}
    assert client.get("/api/runs/run-1/rounds/1/artifacts/../report.json").status_code in {404, 422}
    assert client.post("/api/runs/run-1/instructions", json={"instructions": "keep going"}).status_code == 200
    assert state.list_injections() == ["keep going"]
    queued_snapshot = client.get("/api/runs/run-1/snapshot").json()
    assert queued_snapshot["operator_messages"] == [
        {
            "id": queued_snapshot["operator_messages"][0]["id"],
            "text": "keep going",
            "created_at": queued_snapshot["operator_messages"][0]["created_at"],
            "status": "queued",
        }
    ]
    state.drain_injections()
    applied_snapshot = client.get("/api/runs/run-1/snapshot").json()
    assert applied_snapshot["operator_messages"][0]["text"] == "keep going"
    assert applied_snapshot["operator_messages"][0]["status"] == "applied"
    resolved = client.post(
        f"/api/runs/run-1/approvals/{approval.approval_id}/resolve",
        json={"action": "continue"},
    )
    assert resolved.status_code == 200
    assert state.get_approval(approval.approval_id).status == "resolved"


def test_api_serves_binary_artifact_inline(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path)
    screenshot = b"\x89PNG\r\n\x1a\n\x00binary-screenshot"
    round_dir = root / "run-1" / "logs" / "role_management" / "rounds" / "round_001"
    (round_dir / "screenshot.png").write_bytes(screenshot)
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    artifact = client.get("/api/runs/run-1/rounds/1/artifacts/screenshot.png/raw")

    assert artifact.status_code == 200
    assert artifact.headers["content-type"] == "image/png"
    assert artifact.headers["content-disposition"].startswith("inline;")
    assert artifact.content == screenshot
    assert client.get("/api/runs/run-1/rounds/1/artifacts/%2E%2E/raw").status_code == 404


def test_api_does_not_execute_html_artifacts(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path)
    html = root / "run-1" / "logs" / "role_management" / "rounds" / "round_001" / "result.html"
    html.write_text("<script>window.pwned = true</script>", encoding="utf-8")
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    raw = client.get("/api/runs/run-1/rounds/1/artifacts/result.html/raw")
    text = client.get("/api/runs/run-1/rounds/1/artifacts/result.html")

    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("text/plain")
    assert raw.headers["content-disposition"].startswith("attachment;")
    assert raw.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in raw.headers["content-security-policy"]
    assert text.headers["content-type"].startswith("text/plain")
    assert text.headers["x-content-type-options"] == "nosniff"


def test_approval_cannot_be_queued_twice(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path)
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))
    approval = state.create_approval(title="Continue fixture")
    first = client.post(
        f"/api/runs/run-1/approvals/{approval.approval_id}/resolve",
        headers={"Idempotency-Key": "approval-1"},
        json={"action": "continue"},
    )
    second = client.post(
        f"/api/runs/run-1/approvals/{approval.approval_id}/resolve",
        headers={"Idempotency-Key": "approval-2"},
        json={"action": "continue"},
    )
    assert first.status_code == 200
    assert second.status_code == 409
    assert len(state.control_bus.commands()) == 1


def test_invalid_durable_approval_command_is_rejected_without_crashing(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path)
    approval = state.create_approval(title="Continue fixture")
    state.control_bus.append(
        "resolve_approval",
        {"approval_id": approval.approval_id, "action": "not-an-option"},
        created_by="operator",
        command_id="invalid-approval",
    )

    current = state.get_approval(approval.approval_id)

    assert current is not None
    assert current.status == "pending"
    receipt = state.control_bus.receipt_for("invalid-approval")
    assert receipt is not None
    assert receipt["status"] == "rejected"
    assert receipt["message"] == "invalid approval response"


def test_instruction_idempotency_key_conflict_is_not_silently_replayed(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path)
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))
    first = client.post(
        "/api/runs/run-1/instructions",
        headers={"Idempotency-Key": "instruction-1"},
        json={"instructions": "first"},
    )
    conflicting = client.post(
        "/api/runs/run-1/instructions",
        headers={"Idempotency-Key": "instruction-1"},
        json={"instructions": "different"},
    )
    assert first.status_code == 200
    assert conflicting.status_code == 409
    assert state.list_injections() == ["first"]


def test_websocket_starts_with_snapshot_and_replay(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path)
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))
    with client.websocket_connect("/api/runs/run-1/stream?replay=2") as websocket:
        assert websocket.receive_json()["kind"] == "snapshot"
        first = websocket.receive_json()
        second = websocket.receive_json()
        assert first["kind"] == second["kind"] == "event"
        assert first["data"]["event_id"] != second["data"]["event_id"]

        events_path = root / "run-1" / "logs" / "role_management" / "events.jsonl"
        threading.Timer(
            0.1,
            lambda: events_path.open("a", encoding="utf-8").write(
                json.dumps({"ts": 3, "event": "manager_round_done", "round_index": 1}) + "\n"
            ),
        ).start()
        live_event = websocket.receive_json()
        live_snapshot = websocket.receive_json()
        assert live_event["kind"] == "event"
        assert live_event["data"]["type"] == "round.manager.completed"
        assert live_snapshot["kind"] == "snapshot"


def test_websocket_publishes_operator_messages_without_role_events(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path)
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))
    with client.websocket_connect("/api/runs/run-1/stream?replay=0") as websocket:
        assert websocket.receive_json()["kind"] == "snapshot"
        threading.Timer(
            0.1,
            lambda: state.add_injection("Show this immediately", command_id="instruction-live"),
        ).start()
        update = websocket.receive_json()
        assert update["kind"] == "snapshot"
        assert update["data"]["operator_messages"] == [{
            "id": "instruction-live",
            "text": "Show this immediately",
            "created_at": update["data"]["operator_messages"][0]["created_at"],
            "status": "queued",
        }]


def test_websocket_publishes_resolved_approval_answer_without_role_events(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path)
    approval = state.create_approval(title="Choose an account", message="A or B?")
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    def answer() -> None:
        assert state.resolve_approval(
            approval.approval_id,
            action="continue",
            user_input="Account B",
            command_id="approval-answer-live",
        )
        state.get_approval(approval.approval_id)

    with client.websocket_connect("/api/runs/run-1/stream?replay=0") as websocket:
        assert websocket.receive_json()["kind"] == "snapshot"
        threading.Timer(0.1, answer).start()
        update = websocket.receive_json()
        resolved = next(item for item in update["data"]["approvals"] if item["approval_id"] == approval.approval_id)
        assert update["kind"] == "snapshot"
        assert resolved["status"] == "resolved"
        assert resolved["action"] == "continue"
        assert resolved["user_input"] == "Account B"
