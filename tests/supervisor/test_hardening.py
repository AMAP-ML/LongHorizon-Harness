from __future__ import annotations

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import lh_harness.supervisor.control_bus as control_bus
import lh_harness.supervisor.service as supervisor_service
from lh_harness.supervisor.control_bus import ControlBus, RevisionConflict, iter_run_control_dirs
from lh_harness.supervisor.service import IdempotencyConflict, RunSupervisor


class _Process:
    pid = 9191

    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


def test_nonzero_worker_exit_is_failed_and_persists_crash_report(monkeypatch, tmp_path: Path) -> None:
    process = _Process(7)
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    created = supervisor.create_run(task="crash me")
    run_dir = tmp_path / "runs" / created["id"]
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs" / "report.json").write_text(
        json.dumps({"status": "complete", "completion_satisfied": True}), encoding="utf-8"
    )

    status = supervisor.status(created["id"])

    assert status["status"] == "failed"
    assert status["exit_code"] == 7
    assert status["report_status"] == "completed"
    crash = json.loads((run_dir / "logs" / "crash_report.json").read_text(encoding="utf-8"))
    assert crash["status"] == "failed"


@pytest.mark.parametrize("completion_satisfied", [False, None])
def test_success_report_requires_explicit_completion_evidence(
    completion_satisfied: bool | None,
) -> None:
    report: dict[str, object] = {"status": "complete"}
    if completion_satisfied is not None:
        report["completion_satisfied"] = completion_satisfied

    lifecycle, report_status = supervisor_service._terminal_status_for_exit(
        report=report,
        returncode=0,
    )

    assert lifecycle == "failed"
    assert report_status == "completed"


def test_historical_completed_report_without_evidence_projects_failed(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run = root / "run-1"
    (run / "logs" / "role_management").mkdir(parents=True)
    (run / "logs" / "report.json").write_text(
        json.dumps({"status": "complete", "completion_satisfied": False}),
        encoding="utf-8",
    )

    status = RunSupervisor(root, workspace_root=tmp_path / "workspace").status("run-1")

    assert status["status"] == "failed"
    assert status["report_status"] == "completed"
    assert status["failure_reason"] == "worker reported completion without explicit completion evidence"


def test_failure_report_does_not_follow_existing_report_symlink(monkeypatch, tmp_path: Path) -> None:
    process = _Process(7)
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    created = supervisor.create_run(task="crash without redirect")
    run_dir = tmp_path / "runs" / created["id"]
    logs = run_dir / "logs"
    outside = tmp_path / "outside-report.json"
    outside.write_text("private", encoding="utf-8")
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "report.json").unlink(missing_ok=True)
    (logs / "report.json").symlink_to(outside)

    supervisor.status(created["id"])

    assert outside.read_text(encoding="utf-8") == "private"
    assert not (logs / "report.json").is_symlink()
    assert json.loads((logs / "report.json").read_text(encoding="utf-8"))["status"] == "failed"


def test_resume_rejects_active_run(monkeypatch, tmp_path: Path) -> None:
    process = _Process()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    created = supervisor.create_run(task="still running")

    with pytest.raises(ValueError, match="active"):
        supervisor.resume(created["id"])


def test_workspace_must_stay_inside_configured_root(monkeypatch, tmp_path: Path) -> None:
    process = _Process()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    root = tmp_path / "workspace"
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=root)

    with pytest.raises(ValueError, match="inside configured workspace root"):
        supervisor.create_run(task="escape", workspace=str(tmp_path / "outside"))

    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir(parents=True, exist_ok=True)
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="inside configured workspace root"):
        supervisor.create_run(task="symlink escape", workspace=str(root / "link"))


def test_control_bus_revisions_and_expected_revision_are_atomic(tmp_path: Path) -> None:
    path = tmp_path / "run"

    def append_many(worker: int) -> list[int]:
        bus = ControlBus(path)
        return [bus.append("test", {"worker": worker, "index": index})["revision"] for index in range(20)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        revisions = [revision for group in executor.map(append_many, range(8)) for revision in group]
    assert sorted(revisions) == list(range(1, 161))

    with pytest.raises(RevisionConflict):
        ControlBus(path).append("stale", expected_revision=0)
    bus = ControlBus(path)
    command = bus.append("idempotent", command_id="same")
    assert bus.append("idempotent", command_id="same")["revision"] == command["revision"]

    def write_status(index: int) -> None:
        ControlBus(path).write_status({"writer": index})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write_status, range(40)))
    assert ControlBus(path).read_status().get("writer") is not None


def test_control_bus_accepts_trusted_macos_var_alias(tmp_path: Path) -> None:
    """The system /var alias must not be confused with a run-boundary link."""

    alias = Path("/var")
    real = Path(os.path.realpath(alias))
    if not alias.is_symlink():
        pytest.skip("host has no macOS /var alias")
    try:
        relative = tmp_path.resolve().relative_to(real)
    except ValueError:
        pytest.skip("pytest temporary root is outside /var")
    run = alias / relative / "alias-run"
    command = ControlBus(run).append("alias-safe")
    assert command["revision"] == 1


def test_control_bus_fails_closed_when_process_lock_breaks(monkeypatch, tmp_path: Path) -> None:
    import fcntl

    def broken_flock(*_args, **_kwargs):
        raise OSError("lock unavailable")

    monkeypatch.setattr(fcntl, "flock", broken_flock)
    with pytest.raises(RuntimeError, match="locking is unavailable"):
        ControlBus(tmp_path / "run").append("test")


def test_supervisor_idempotency_lock_rejects_symlink(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    outside = tmp_path / "outside.lock"
    outside.write_text("private", encoding="utf-8")
    (root / ".supervisor.lock").symlink_to(outside)
    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace")

    with pytest.raises(RuntimeError, match="secure supervisor locking"):
        with supervisor._supervisor_locked():
            pass

    assert outside.read_text(encoding="utf-8") == "private"


def test_control_bus_append_rejects_hardlinked_control_log(tmp_path: Path) -> None:
    run = tmp_path / "run"
    control = run / "control"
    control.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    try:
        (control / "commands.jsonl").hardlink_to(outside)
    except OSError:
        pytest.skip("filesystem does not support hard links")

    with pytest.raises(OSError):
        ControlBus(run).append("blocked")
    assert outside.read_text(encoding="utf-8") == ""


def test_control_bus_rejects_symlinked_control_directory(tmp_path: Path) -> None:
    run = tmp_path / "run"
    outside = tmp_path / "outside-control"
    outside.mkdir()
    run.mkdir()
    (run / "control").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        ControlBus(run).append("blocked")
    assert list(outside.iterdir()) == []


def test_control_bus_rejects_symlinked_run_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside-run"
    outside.mkdir()
    run_link = tmp_path / "run-link"
    run_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        ControlBus(run_link).append("blocked")

    assert list(outside.iterdir()) == []


def test_idempotency_write_rejects_symlinked_directory(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    outside = tmp_path / "outside-idempotency"
    outside.mkdir()
    (root / ".idempotency").symlink_to(outside, target_is_directory=True)
    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace")

    with pytest.raises(OSError):
        supervisor._write_idempotency(
            supervisor._idempotency_path("create", "key"),
            {"state": "creating"},
        )
    assert list(outside.iterdir()) == []


def test_task_file_write_rejects_symlinked_tmp_directory(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    outside = tmp_path / "outside-tmp"
    outside.mkdir()
    (run / "tmp").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        supervisor_service.RunSupervisor._write_task_file(run / "tmp" / "task.md", "secret")
    assert list(outside.iterdir()) == []


def test_create_recovery_rejects_tmp_symlink_before_worker_launch(
    monkeypatch, tmp_path: Path
) -> None:
    """A recovered reservation must not let ``tmp`` redirect task.md writes."""

    launches: list[object] = []

    def launch(*args, **kwargs):
        launches.append((args, kwargs))
        return _Process()

    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", launch)
    root = tmp_path / "runs"
    run = root / "reserved"
    run.mkdir(parents=True)
    outside = tmp_path / "outside-tmp"
    outside.mkdir()
    (run / "tmp").symlink_to(outside, target_is_directory=True)
    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace")

    with pytest.raises(OSError):
        supervisor._create_run_once(
            task="must stay inside the reservation",
            run_id="reserved",
            _recover_reservation=True,
        )

    assert launches == []
    assert list(outside.iterdir()) == []


def test_control_bus_lock_rejects_hardlink_alias(tmp_path: Path) -> None:
    """A lock alias would let two boundaries serialize independently."""

    run = tmp_path / "run"
    control = run / "control"
    control.mkdir(parents=True)
    outside = tmp_path / "outside.lock"
    outside.write_text("private", encoding="utf-8")
    try:
        (control / ".control.lock").hardlink_to(outside)
    except OSError:
        pytest.skip("filesystem does not support hard links")

    with pytest.raises(RuntimeError, match="locking is unavailable"):
        ControlBus(run).append("blocked")
    assert outside.read_text(encoding="utf-8") == "private"


def test_iter_run_control_dirs_skips_symlinked_run_boundaries(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    real = root / "real" / "control"
    real.mkdir(parents=True)
    outside = tmp_path / "outside" / "control"
    outside.mkdir(parents=True)
    (root / "evil").symlink_to(outside.parent, target_is_directory=True)

    assert list(iter_run_control_dirs(root)) == [root / "real"]


def test_atomic_json_write_closes_mkstemp_fd_when_fdopen_fails(monkeypatch, tmp_path: Path) -> None:
    """The temporary descriptor must not leak if wrapping it raises."""

    captured: dict[str, int] = {}
    real_fdopen = control_bus.os.fdopen

    def broken_fdopen(fd: int, *args, **kwargs):
        captured["fd"] = fd
        raise OSError("fdopen failed")

    monkeypatch.setattr(control_bus.os, "fdopen", broken_fdopen)
    target = tmp_path / "status.json"
    with pytest.raises(OSError, match="fdopen failed"):
        ControlBus._atomic_json_write(target, {"status": "testing"})

    assert "fd" in captured
    with pytest.raises(OSError):
        os.fstat(captured["fd"])
    # Keep the test isolated if a platform's fdopen implementation behaves
    # differently under monkeypatching; restoring is also useful for debuggers.
    monkeypatch.setattr(control_bus.os, "fdopen", real_fdopen)


def test_stop_keeps_stopping_state_while_stale_approval_is_present(
    monkeypatch, tmp_path: Path
) -> None:
    """A stop request must not be hidden by the approval projection."""

    process = _Process()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("lh_harness.supervisor.service.os.killpg", lambda *args, **kwargs: None)
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    created = supervisor.create_run(task="stop me")
    run_dir = tmp_path / "runs" / created["id"]
    role_dir = run_dir / "logs" / "role_management"
    role_dir.mkdir(parents=True, exist_ok=True)
    (role_dir / "approvals.jsonl").write_text(
        json.dumps({"approval_id": "approval-1", "status": "pending"}) + "\n",
        encoding="utf-8",
    )

    assert supervisor.status(created["id"])["status"] == "waiting_approval"
    supervisor.stop(created["id"])
    status = supervisor.status(created["id"])

    assert status["status"] == "stopping"
    assert status["requested_action"] == "stop"
    assert status["alive"] is True


def test_abort_escalates_stop_and_cross_action_retries_are_idempotent(
    monkeypatch, tmp_path: Path
) -> None:
    process = _Process()
    signals: list[object] = []
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("lh_harness.supervisor.service.os.killpg", lambda _pid, sig: signals.append(sig))
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    created = supervisor.create_run(task="stop then abort")

    stopped = supervisor.stop(created["id"])
    aborted = supervisor.abort(created["id"])
    repeated_stop = supervisor.stop(created["id"])

    assert stopped["signal"] == "SIGTERM"
    assert aborted["signal"] == "SIGKILL"
    assert repeated_stop["idempotent"] is True
    assert signals == [supervisor_service.signal.SIGTERM, supervisor_service.signal.SIGKILL]
    status = supervisor.status(created["id"])
    assert status["status"] == "stopping"
    assert status["requested_action"] == "abort"


def test_stop_after_abort_returns_abort_receipt_without_conflict(monkeypatch, tmp_path: Path) -> None:
    process = _Process()
    signals: list[object] = []
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("lh_harness.supervisor.service.os.killpg", lambda _pid, sig: signals.append(sig))
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    created = supervisor.create_run(task="abort then stop")

    supervisor.abort(created["id"])
    repeated = supervisor.stop(created["id"])

    assert repeated["command_id"] == "lifecycle-abort"
    assert repeated["signal"] == "SIGKILL"
    assert repeated["idempotent"] is True
    assert signals == [supervisor_service.signal.SIGKILL]


def test_first_role_event_promotes_starting_worker_to_running(monkeypatch, tmp_path: Path) -> None:
    process = _Process()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    created = supervisor.create_run(task="promote after first event")
    run_dir = tmp_path / "runs" / created["id"]

    assert ControlBus(run_dir).read_status()["status"] == "starting"
    role_dir = run_dir / "logs" / "role_management"
    role_dir.mkdir(parents=True)
    (role_dir / "events.jsonl").write_text(
        json.dumps({"event": "role_harness_start"}) + "\n",
        encoding="utf-8",
    )

    assert supervisor.status(created["id"])["status"] == "running"


def test_stale_status_poll_cannot_overwrite_concurrent_stop(
    monkeypatch, tmp_path: Path
) -> None:
    """The read/merge/write lifecycle update is serialized across processes."""

    process = _Process()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    created = supervisor.create_run(task="race status")
    entered = threading.Event()
    release = threading.Event()

    def delayed_pending(_path: Path) -> bool:
        entered.set()
        assert release.wait(timeout=3)
        return False

    monkeypatch.setattr("lh_harness.supervisor.service._pending_approval", delayed_pending)
    result: dict[str, object] = {}

    def poll() -> None:
        result.update(supervisor.status(created["id"]))

    thread = threading.Thread(target=poll)
    thread.start()
    assert entered.wait(timeout=3)

    bus = ControlBus(tmp_path / "runs" / created["id"])
    current = bus.read_status()
    bus.write_status(
        {
            **current,
            "status": "stopping",
            "requested_action": "stop",
            "stop_requested_at": 1.0,
        }
    )
    release.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert result["status"] == "stopping"
    assert bus.read_status()["status"] == "stopping"


def test_attached_supervisor_is_scoped_to_one_run_and_rejects_other_run(
    tmp_path: Path,
) -> None:
    supervisor = RunSupervisor(
        tmp_path / "runs",
        workspace_root=tmp_path / "workspace",
        attached_only=True,
    )
    owner = supervisor.attach_run(run_id="run-1", pid=os.getpid(), task="attached")
    assert owner["attached"] is True
    assert supervisor.attached_run_id == "run-1"
    assert supervisor.list_run_items()[0]["id"] == "run-1"
    with pytest.raises(ValueError, match="another run"):
        supervisor.status("run-2")
    with pytest.raises(ValueError, match="another run"):
        supervisor.attach_run(run_id="run-2", pid=os.getpid())


def test_attached_stop_queues_cooperative_cancellation_without_signalling_self(
    monkeypatch, tmp_path: Path
) -> None:
    signals: list[tuple[int, int]] = []

    def record_signal(pid: int, sig: int) -> None:
        if sig != 0:
            signals.append((pid, sig))

    monkeypatch.setattr(supervisor_service.os, "kill", record_signal)
    supervisor = RunSupervisor(
        tmp_path / "runs",
        workspace_root=tmp_path / "workspace",
        attached_only=True,
    )
    supervisor.attach_run(run_id="run-1", pid=os.getpid(), task="stop me")

    result = supervisor.stop("run-1")

    assert result["status"] == "accepted"
    assert signals == []
    status = supervisor.status("run-1")
    assert status["status"] == "stopping"
    assert status["requested_action"] == "stop"


def test_finalize_attached_run_persists_terminal_state_before_keep_dashboard(tmp_path: Path) -> None:
    supervisor = RunSupervisor(
        tmp_path / "runs",
        workspace_root=tmp_path / "workspace",
        attached_only=True,
    )
    supervisor.attach_run(
        run_id="run-1",
        pid=os.getpid(),
        task="finish",
        command=["lh-harness", "run"],
    )

    status = supervisor.finalize_attached_run(
        "run-1",
        report={"status": "complete", "completion_satisfied": True},
        returncode=0,
    )
    owner = supervisor.owner("run-1")

    assert status["status"] == "completed"
    assert status["alive"] is False
    assert owner["state"] == "completed"
    assert owner["managed"] is False
    assert owner["pid"] == owner["pgid"] == 0
    # Terminal status remains monotonic even though this process is still alive
    # to serve a kept dashboard.
    assert supervisor.status("run-1")["status"] == "completed"


def test_stop_persists_intent_before_signal(monkeypatch, tmp_path: Path) -> None:
    process = _Process()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    observed: list[str] = []

    def killpg(_pid: int, _sig: object) -> None:
        observed.append(ControlBus(tmp_path / "runs" / created["id"]).read_status().get("status", ""))

    monkeypatch.setattr("lh_harness.supervisor.service.os.killpg", killpg)
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    created = supervisor.create_run(task="ordering")
    supervisor.stop(created["id"])
    assert observed == ["stopping"]


def test_signal_process_lookup_reconciles_stopping_to_failed(monkeypatch, tmp_path: Path) -> None:
    process = _Process()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("lh_harness.supervisor.service.os.killpg", lambda *args, **kwargs: (_ for _ in ()).throw(ProcessLookupError()))
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    created = supervisor.create_run(task="vanish")

    with pytest.raises(ValueError, match="no longer running"):
        supervisor.stop(created["id"])
    status = supervisor.status(created["id"])
    assert status["status"] == "failed"
    assert status["alive"] is False
    assert status["requested_action"] == "stop"
    assert supervisor.command_receipt(created["id"], "lifecycle-stop")["status"] == "rejected"


def test_signal_permission_failure_restores_active_state(monkeypatch, tmp_path: Path) -> None:
    process = _Process()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("lh_harness.supervisor.service.os.killpg", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError()))
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    created = supervisor.create_run(task="permission")

    with pytest.raises(ValueError, match="permission denied"):
        supervisor.stop(created["id"])
    status = supervisor.status(created["id"])
    assert status["status"] in {"starting", "running"}
    assert status["alive"] is True
    assert "requested_action" not in status
    assert supervisor.command_receipt(created["id"], "lifecycle-stop")["status"] == "failed"


def test_pending_lifecycle_command_is_replayed_after_supervisor_restart(monkeypatch, tmp_path: Path) -> None:
    process = _Process()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    sent: list[object] = []
    monkeypatch.setattr("lh_harness.supervisor.service.os.killpg", lambda pid, sig: sent.append((pid, sig)))
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    created = supervisor.create_run(task="replay stop")
    bus = ControlBus(tmp_path / "runs" / created["id"])
    current = bus.read_status()
    bus.append("stop", {"signal": "SIGTERM"}, command_id="lifecycle-stop")
    bus.write_status({**current, "status": "stopping", "requested_action": "stop", "alive": True})

    # A fresh supervisor has no Popen handle, but the durable command must be
    # delivered once the PID identity can still be checked.
    restarted = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    monkeypatch.setattr(restarted, "_is_alive", lambda _run_id: True)
    status = restarted.status(created["id"])
    assert sent
    assert bus.receipt_for("lifecycle-stop")["status"] == "accepted"
    assert status["status"] == "stopping"


def test_create_idempotency_reuses_worker_and_rejects_conflicting_payload(monkeypatch, tmp_path: Path) -> None:
    process = _Process()
    launches: list[object] = []

    def launch(*args, **kwargs):
        launches.append(True)
        return process

    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", launch)
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    first = supervisor.create_run(task="same", idempotency_key="create-1")
    second = supervisor.create_run(task="same", idempotency_key="create-1")
    assert first["id"] == second["id"]
    assert second["idempotent"] is True
    assert len(launches) == 1
    with pytest.raises(IdempotencyConflict):
        supervisor.create_run(task="different", idempotency_key="create-1")


def test_worker_command_forwards_selected_agent_and_model(monkeypatch, tmp_path: Path) -> None:
    process = _Process()
    commands: list[list[str]] = []
    launch_env: dict[str, str] = {}

    def launch(command, *args, **kwargs):
        commands.append(list(command))
        launch_env.update(kwargs.get("env", {}))
        return process

    monkeypatch.setenv("PYTHONPATH", "relative-source")
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", launch)
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    created = supervisor.create_run(
        task="use the selected backend",
        agent="claude_code",
        model="claude-custom-test",
        role_configs={
            "manager": {"agent": "codex", "model": "gpt-manager-test"},
            "executor": {"agent": "claude_code", "model": "claude-executor-test"},
            "auditor": {"agent": "codex", "model": "gpt-auditor-test"},
        },
    )

    assert created["owner"]["agent"] == "claude_code"
    assert created["owner"]["model"] == "claude-custom-test"
    assert created["owner"]["role_configs"]["auditor"] == {
        "agent": "codex",
        "model": "gpt-auditor-test",
    }
    assert len(commands) == 1
    command = commands[0]
    assert command[:3] == [sys.executable, "-m", "lh_harness"]
    package_root = str(Path(supervisor_service.__file__).resolve().parents[2])
    assert launch_env["PYTHONPATH"].split(os.pathsep)[0] == package_root
    assert "relative-source" in launch_env["PYTHONPATH"].split(os.pathsep)[1:]
    assert "--agent=claude_code" in command
    assert "--model=claude-custom-test" in command
    assert "--manager-agent=codex" in command
    assert "--manager-model=gpt-manager-test" in command
    assert "--executor-agent=claude_code" in command
    assert "--executor-model=claude-executor-test" in command
    assert "--auditor-agent=codex" in command
    assert "--auditor-model=gpt-auditor-test" in command
    task_argument = next(item.removeprefix("--task=") for item in command if item.startswith("--task="))
    assert task_argument.startswith("@")
    assert Path(task_argument[1:]).read_text(encoding="utf-8").strip() == "use the selected backend"
    assert "use the selected backend" not in supervisor.owner(created["id"]).get("command_display", "")


def test_fresh_idempotency_key_cannot_adopt_preexisting_run(monkeypatch, tmp_path: Path) -> None:
    process = _Process()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    root = tmp_path / "runs"
    existing = root / "taken"
    existing.mkdir(parents=True)
    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace")

    with pytest.raises(ValueError, match="already exists"):
        supervisor.create_run(
            task="must not adopt",
            run_id="taken",
            idempotency_key="fresh-key",
        )
    assert not supervisor._idempotency_path("create", "fresh-key").exists()


def test_worker_does_not_inherit_web_control_token(monkeypatch, tmp_path: Path) -> None:
    """The agent must not be able to read the API bearer credential."""

    process = _Process()
    captured: dict[str, object] = {}

    def launch(*args, **kwargs):
        captured.update(kwargs)
        return process

    monkeypatch.setenv("LH_HARNESS_WEB_TOKEN", "control-secret")
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", launch)
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    supervisor.create_run(task="do not leak the control token")

    worker_env = captured.get("env")
    assert isinstance(worker_env, dict)
    assert worker_env.get("LH_HARNESS_WEB_TOKEN") is None


def test_run_listing_skips_symlinked_run_dirs(monkeypatch, tmp_path: Path) -> None:
    process = _Process()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process)
    root = tmp_path / "runs"
    outside = tmp_path / "outside"
    (outside / "logs" / "role_management").mkdir(parents=True)
    (root).mkdir(parents=True)
    (root / "evil").symlink_to(outside, target_is_directory=True)
    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace")
    assert all(item["id"] != "evil" for item in supervisor.list_run_items())


def test_worker_log_open_rejects_final_symlink(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("private", encoding="utf-8")
    link = run_dir / "worker.log"
    link.symlink_to(outside)

    with pytest.raises(OSError):
        supervisor_service._open_worker_log(link)

    assert outside.read_text(encoding="utf-8") == "private"


def test_worker_log_open_rejects_hardlink_alias(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("private", encoding="utf-8")
    alias = run_dir / "worker.log"
    try:
        alias.hardlink_to(outside)
    except OSError:
        pytest.skip("filesystem does not support hard links")

    with pytest.raises(OSError):
        supervisor_service._open_worker_log(alias)

    assert outside.read_text(encoding="utf-8") == "private"


def test_worker_log_open_compacts_old_tail_and_tightens_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor_service, "_MAX_WORKER_LOG_BYTES", 64)
    monkeypatch.setattr(supervisor_service, "_WORKER_LOG_KEEP_BYTES", 32)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    path = run_dir / "worker.log"
    path.write_bytes(bytes(range(100)))
    path.chmod(0o644)

    output = supervisor_service._open_worker_log(path)
    try:
        output.seek(0)
        assert output.read() == bytes(range(68, 100))
        assert path.stat().st_size == 32
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        output.close()


@pytest.mark.parametrize("child", ["role_management", "rounds"])
def test_run_listing_rejects_nested_role_boundary_symlinks(tmp_path: Path, child: str) -> None:
    root = tmp_path / "runs"
    run = root / "run-1"
    role = run / "logs" / "role_management"
    role.mkdir(parents=True)
    if child == "role_management":
        real = tmp_path / "real-role"
        real.mkdir()
        role.rmdir()
        role.symlink_to(real, target_is_directory=True)
    else:
        rounds = role / "rounds"
        rounds.mkdir()
        real = tmp_path / "real-rounds"
        real.mkdir()
        rounds.rmdir()
        rounds.symlink_to(real, target_is_directory=True)
    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace")
    assert all(item["id"] != "run-1" for item in supervisor.list_run_items())


@pytest.mark.parametrize("target_inside_root", [True, False])
def test_run_listing_rejects_cross_run_or_external_logs_symlinks(
    tmp_path: Path,
    target_inside_root: bool,
) -> None:
    root = tmp_path / "runs"
    run = root / "run-1"
    sibling_logs = root / "run-2" / "logs" / "role_management"
    sibling_logs.mkdir(parents=True)
    outside_logs = tmp_path / "outside" / "logs" / "role_management"
    outside_logs.mkdir(parents=True)
    target = sibling_logs.parent if target_inside_root else outside_logs.parent
    run.mkdir(parents=True)
    (run / "control").mkdir()
    (run / "logs").symlink_to(target, target_is_directory=True)

    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace")

    assert all(item["id"] != "run-1" for item in supervisor.list_run_items())


def test_run_listing_does_not_follow_final_task_contract_symlink(tmp_path: Path) -> None:
    """A worker-writable contract pathname must not disclose an outside file."""

    root = tmp_path / "runs"
    run = root / "run-1"
    rounds = run / "logs" / "role_management" / "rounds"
    rounds.mkdir(parents=True)
    (run / "control").mkdir()
    outside = tmp_path / "secret-task.txt"
    outside.write_text("do not disclose this secret", encoding="utf-8")
    (rounds / "round_001").mkdir()
    (rounds / "round_001" / "task_contract.txt").symlink_to(outside)

    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace")
    items = supervisor.list_run_items()

    item = next(item for item in items if item["id"] == "run-1")
    assert item["task"] == ""
    assert "secret" not in json.dumps(item, ensure_ascii=False)


def test_resume_does_not_follow_final_task_contract_symlink(tmp_path: Path) -> None:
    """Resume must fail closed instead of using a secret linked contract."""

    root = tmp_path / "runs"
    run = root / "run-1"
    rounds = run / "logs" / "role_management" / "rounds"
    rounds.mkdir(parents=True)
    (run / "control").mkdir()
    outside = tmp_path / "secret-task.txt"
    outside.write_text("secret resume task", encoding="utf-8")
    (rounds / "round_001").mkdir()
    (rounds / "round_001" / "task_contract.txt").symlink_to(outside)
    bus = ControlBus(run)
    bus.write_owner(
        {
            "run_id": "run-1",
            "task": "",
            "agent": "codex",
            "model": None,
            "max_rounds": 30,
            "workspace": str(tmp_path / "workspace"),
            "pid": 0,
        }
    )
    bus.write_status({"run_id": "run-1", "status": "completed", "alive": False})

    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace")
    with pytest.raises(ValueError, match="without a saved task"):
        supervisor.resume("run-1")


def test_saved_task_contract_accepts_bounded_regular_file(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run = root / "run-1"
    rounds = run / "logs" / "role_management" / "rounds"
    contract = rounds / "round_002" / "task_contract.txt"
    contract.parent.mkdir(parents=True)
    contract.write_text("first line\nsecond line\n", encoding="utf-8")

    assert supervisor_service._saved_task_from_rounds(root, "run-1", first_line=True) == "first line"
    assert supervisor_service._saved_task_from_rounds(root, "run-1", first_line=False) == "first line\nsecond line"


def test_approval_persistence_rejects_final_symlink(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run = root / "run-1"
    role = run / "logs" / "role_management"
    role.mkdir(parents=True)
    outside = tmp_path / "outside-approvals.jsonl"
    outside.write_text("private\n", encoding="utf-8")
    (role / "approvals.jsonl").symlink_to(outside)

    from lh_harness.dashboard.state import ApprovalOption, DashboardState

    state = DashboardState(run / "logs", runs_root=root, control_enabled=True)
    state.create_approval(
        title="should not escape",
        options=[ApprovalOption("continue", "Continue")],
    )
    assert outside.read_text(encoding="utf-8") == "private\n"


@pytest.mark.parametrize("target_inside_root", [True, False])
def test_resume_rejects_cross_run_or_external_logs_symlinks(
    tmp_path: Path,
    target_inside_root: bool,
) -> None:
    root = tmp_path / "runs"
    run = root / "run-1"
    sibling_logs = root / "run-2" / "logs" / "role_management"
    sibling_logs.mkdir(parents=True)
    (sibling_logs / "task_contract.txt").write_text("secret sibling task", encoding="utf-8")
    outside_logs = tmp_path / "outside" / "logs" / "role_management"
    outside_logs.mkdir(parents=True)
    (outside_logs / "task_contract.txt").write_text("secret external task", encoding="utf-8")
    target = sibling_logs.parent if target_inside_root else outside_logs.parent
    run.mkdir(parents=True)
    (run / "logs").symlink_to(target, target_is_directory=True)
    bus = ControlBus(run)
    bus.write_owner({
        "run_id": "run-1",
        "task": "original task",
        "agent": "codex",
        "max_rounds": 30,
        "workspace": str(tmp_path / "workspace"),
        "pid": 0,
    })
    bus.write_status({"run_id": "run-1", "status": "completed", "alive": False})

    supervisor = RunSupervisor(root, workspace_root=tmp_path / "workspace")

    with pytest.raises(ValueError, match="logs path"):
        supervisor.resume("run-1")
