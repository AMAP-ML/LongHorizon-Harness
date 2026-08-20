from __future__ import annotations

import json
import asyncio
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import lh_harness.cli as cli
from lh_harness.cli import (
    _adopt_supervised_run_dir,
    _claim_supervised_owner,
    _read_supervised_task,
    _read_task,
    _reserve_run_dir,
    _run_with_attached_control,
    _open_browser_when_ready,
    _wait_for_dashboard_ready,
    _should_keep_embedded_dashboard,
    _write_bootstrap_failure,
)


def _dashboard_args(tmp_path: Path, **overrides: object):
    values = {
        "runs_root": str(tmp_path / "runs"),
        "log_dir": None,
        "workspace_root": str(tmp_path),
        "host": "127.0.0.1",
        "port": 8799,
        "no_open": True,
        "auth_token": None,
    }
    values.update(overrides)
    return cli.argparse.Namespace(**values)


def test_dashboard_command_prefers_web_workbench(monkeypatch, tmp_path: Path) -> None:
    received: dict[str, object] = {}

    def run_web_server(**kwargs: object) -> int:
        received.update(kwargs)
        return 17

    monkeypatch.setattr(cli, "_load_web_server_runner", lambda: run_web_server)
    assert cli._dashboard_command(_dashboard_args(tmp_path)) == 17
    assert received["runs_root"] == str(tmp_path / "runs")
    assert received["workspace_root"] == str(tmp_path)


def test_dashboard_command_stays_strict_when_web_dependencies_are_missing(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    def broken_install():
        raise ImportError("No module named 'fastapi'")

    monkeypatch.setattr(cli, "_load_web_server_runner", broken_install)

    assert cli._dashboard_command(_dashboard_args(tmp_path)) == 2
    assert "Reinstall with" in capsys.readouterr().err


def test_web_command_stays_strict_when_web_dependencies_are_missing(monkeypatch, tmp_path: Path, capsys) -> None:
    def broken_install():
        raise ImportError("No module named 'uvicorn'")

    monkeypatch.setattr(cli, "_load_web_server_runner", broken_install)

    assert cli._web_command(_dashboard_args(tmp_path)) == 2
    assert "Reinstall with" in capsys.readouterr().err


def test_browser_opens_only_after_dashboard_is_ready(monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(cli, "_wait_for_dashboard_ready", lambda _url: events.append("ready") or True)
    monkeypatch.setattr(cli, "_open_browser", lambda _url: events.append("open"))

    _open_browser_when_ready("http://127.0.0.1:8799/")

    assert events == ["ready", "open"]


def test_browser_is_not_opened_when_dashboard_never_becomes_ready(monkeypatch, capsys) -> None:
    opened: list[str] = []
    monkeypatch.setattr(cli, "_wait_for_dashboard_ready", lambda _url: False)
    monkeypatch.setattr(cli, "_open_browser", opened.append)

    _open_browser_when_ready("http://127.0.0.1:8799/")

    assert opened == []
    assert "did not become ready" in capsys.readouterr().err


def test_dashboard_readiness_waits_for_an_http_response() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ready")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    timer = threading.Timer(0.12, server.serve_forever)
    started_at = time.monotonic()
    timer.start()
    try:
        assert _wait_for_dashboard_ready(
            f"http://127.0.0.1:{server.server_port}/",
            timeout=1.0,
            poll_interval=0.01,
        ) is True
        assert time.monotonic() - started_at >= 0.1
    finally:
        server.shutdown()
        server.server_close()
        timer.join(timeout=1)


def test_retired_tui_command_is_not_registered(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["tui"])

    assert exc_info.value.code == 2
    assert "invalid choice: 'tui'" in capsys.readouterr().err


def test_explicit_run_id_cannot_reuse_existing_directory(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    (root / "fixed").mkdir(parents=True)

    with pytest.raises(ValueError, match="already exists"):
        _reserve_run_dir(root, "fixed")


@pytest.mark.parametrize("run_id", ["", ".", "..", "nested/run", "..\\escape", "bad\x00id"])
def test_run_id_is_a_single_safe_path_component(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError, match="run id"):
        _reserve_run_dir(tmp_path / "runs", run_id)


def test_generated_run_id_is_reserved_atomically(tmp_path: Path) -> None:
    run_id, run_dir = _reserve_run_dir(tmp_path / "runs", None)
    assert run_id == run_dir.name
    assert run_dir.is_dir()


def test_embedded_control_watcher_cancels_manager_task(tmp_path: Path) -> None:
    from lh_harness.supervisor.control_bus import ControlBus

    run_dir = tmp_path / "run"
    bus = ControlBus(run_dir)
    bus.write_status({"run_id": "run", "status": "stopping", "requested_action": "abort"})

    async def worker() -> dict[str, object]:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            return {"status": "cancelled"}
        return {"status": "unexpected"}

    assert asyncio.run(_run_with_attached_control(worker(), run_dir=run_dir, enabled=True)) == {"status": "cancelled"}


def test_embedded_control_watcher_survives_isolated_ebadf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One stolen-descriptor EBADF costs one poll; the next poll still cancels."""
    import errno

    from lh_harness.supervisor.control_bus import ControlBus

    run_dir = tmp_path / "run"
    bus = ControlBus(run_dir)
    bus.write_status({"run_id": "run", "status": "stopping", "requested_action": "abort"})

    real_read_status = ControlBus.read_status
    calls: list[int] = []

    def flaky_read_status(self):
        calls.append(1)
        if len(calls) == 1:
            raise OSError(errno.EBADF, "Bad file descriptor")
        return real_read_status(self)

    monkeypatch.setattr(ControlBus, "read_status", flaky_read_status)

    async def worker() -> dict[str, object]:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            return {"status": "cancelled"}
        return {"status": "unexpected"}

    result = asyncio.run(
        _run_with_attached_control(worker(), run_dir=run_dir, enabled=True, poll_interval=0.01)
    )
    assert result == {"status": "cancelled"}
    assert len(calls) >= 2


def test_embedded_control_watcher_propagates_non_ebadf_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permission or I/O failures must surface, not blind the watcher."""
    import errno

    from lh_harness.supervisor.control_bus import ControlBus

    run_dir = tmp_path / "run"
    ControlBus(run_dir)

    def denied_read_status(self):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(ControlBus, "read_status", denied_read_status)

    async def worker() -> dict[str, object]:
        await asyncio.sleep(0.05)
        return {"status": "complete"}

    with pytest.raises(OSError) as excinfo:
        asyncio.run(
            _run_with_attached_control(worker(), run_dir=run_dir, enabled=True, poll_interval=0.01)
        )
    assert excinfo.value.errno == errno.EACCES


@pytest.mark.parametrize("requested_action", ["stop", "abort"])
def test_dashboard_control_stop_keeps_embedded_server_alive(tmp_path: Path, requested_action: str) -> None:
    from lh_harness.supervisor.control_bus import ControlBus

    run_dir = tmp_path / "run"
    ControlBus(run_dir).write_status({"status": "stopping", "requested_action": requested_action})

    assert _should_keep_embedded_dashboard(
        run_dir,
        explicitly_requested=False,
        report={"status": "cancelled"},
    ) is True


def test_dashboard_exits_normally_without_stop_or_keep_request(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"

    assert _should_keep_embedded_dashboard(
        run_dir,
        explicitly_requested=False,
        report={"status": "complete"},
    ) is False
    assert _should_keep_embedded_dashboard(
        run_dir,
        explicitly_requested=True,
        report={"status": "complete"},
    ) is True


def test_supervised_worker_adopts_only_its_own_reservation(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run_dir = root / "reserved"
    (run_dir / "control").mkdir(parents=True)
    owner = {
        "run_id": "reserved",
        "pid": os.getpid(),
        "task": "inspect the result",
        "agent": "codex",
        "model": "gpt-5.6-sol",
        "max_rounds": 4,
        "workspace": str((tmp_path / "workspace").resolve()),
        "state": "creating",
    }
    (run_dir / "control" / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
    (run_dir / "control" / "status.json").write_text(
        json.dumps({"run_id": "reserved", "status": "creating"}), encoding="utf-8"
    )

    adopted = _adopt_supervised_run_dir(
        root,
        "reserved",
        task="inspect the result",
        agent="codex",
        model="gpt-5.6-sol",
        workspace=str(tmp_path / "workspace"),
        max_rounds=4,
    )
    assert adopted == ("reserved", run_dir)

    owner["pid"] = os.getpid() + 1
    (run_dir / "control" / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
    with pytest.raises(ValueError, match="another process"):
        _adopt_supervised_run_dir(
            root,
            "reserved",
            task="inspect the result",
            agent="codex",
            model="gpt-5.6-sol",
            workspace=str(tmp_path / "workspace"),
            max_rounds=4,
        )


def test_supervised_worker_claims_pre_popen_reservation(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run_dir = root / "reserved"
    (run_dir / "control").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    owner = {
        "run_id": "reserved",
        "pid": 0,
        "supervisor_pid": 777,
        "task": "race-free launch",
        "agent": "codex",
        "model": None,
        "max_rounds": 2,
        "workspace": str(workspace),
        "state": "creating",
    }
    (run_dir / "control" / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
    (run_dir / "control" / "status.json").write_text(
        json.dumps({"run_id": "reserved", "status": "creating"}), encoding="utf-8"
    )
    monkeypatch.setattr("lh_harness.cli.os.getppid", lambda: 777)

    assert _adopt_supervised_run_dir(
        root,
        "reserved",
        task="race-free launch",
        agent="codex",
        model=None,
        workspace=workspace,
        max_rounds=2,
    ) == ("reserved", run_dir)
    _claim_supervised_owner("reserved", run_dir)
    claimed = json.loads((run_dir / "control" / "owner.json").read_text(encoding="utf-8"))
    assert claimed["pid"] == os.getpid()
    assert claimed["state"] == "running"


def test_supervised_task_reference_is_bound_to_reserved_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "reserved"
    task_path = run_dir / "tmp" / "task.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside-task.md"
    outside.write_text("secret", encoding="utf-8")

    assert _read_supervised_task(f"@{task_path}", run_dir=run_dir) == "inside"
    with pytest.raises(ValueError, match="reserved run/tmp/task.md"):
        _read_supervised_task(f"@{outside}", run_dir=run_dir)

    link = run_dir / "tmp" / "task-link.md"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="reserved run/tmp/task.md"):
        _read_supervised_task(f"@{link}", run_dir=run_dir)

    task_path.unlink()
    try:
        task_path.hardlink_to(outside)
    except OSError:
        pytest.skip("filesystem does not support hard links")
    with pytest.raises(ValueError, match="private regular file"):
        _read_supervised_task(f"@{task_path}", run_dir=run_dir)


@pytest.mark.parametrize("metadata_name", ["owner.json", "status.json"])
def test_supervised_worker_does_not_follow_metadata_symlink(
    tmp_path: Path,
    metadata_name: str,
) -> None:
    root = tmp_path / "runs"
    run_dir = root / "reserved"
    control = run_dir / "control"
    control.mkdir(parents=True)
    owner = {
        "run_id": "reserved",
        "pid": os.getpid(),
        "task": "inspect",
        "agent": "codex",
        "model": None,
        "max_rounds": 1,
    }
    records = {
        "owner.json": owner,
        "status.json": {"run_id": "reserved", "status": "creating"},
    }
    for name, payload in records.items():
        target = control / name
        if name == metadata_name:
            outside = tmp_path / f"outside-{name}"
            outside.write_text(json.dumps(payload), encoding="utf-8")
            target.symlink_to(outside)
        else:
            target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="reservation is invalid"):
        _adopt_supervised_run_dir(
            root,
            "reserved",
            task="inspect",
            agent="codex",
            model=None,
            workspace=None,
            max_rounds=1,
        )


def test_bootstrap_failure_does_not_follow_role_directory_symlink(tmp_path: Path) -> None:
    log_dir = tmp_path / "lh_harness"
    outside = tmp_path / "outside-role"
    log_dir.mkdir()
    outside.mkdir()
    outside_report = outside / "report.json"
    outside_events = outside / "events.jsonl"
    outside_report.write_text("private report", encoding="utf-8")
    outside_events.write_text("private events", encoding="utf-8")
    (log_dir / "role_orchestration").symlink_to(outside, target_is_directory=True)

    _write_bootstrap_failure(log_dir, "task", RuntimeError("boom"), max_rounds=3)

    assert outside_report.read_text(encoding="utf-8") == "private report"
    assert outside_events.read_text(encoding="utf-8") == "private events"
    assert (log_dir / "report.json").is_file()


def test_task_file_reader_does_not_follow_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside-task.md"
    outside.write_text("do not consume", encoding="utf-8")
    link = tmp_path / "task.md"
    link.symlink_to(outside)

    with pytest.raises(OSError):
        _read_task(f"@{link}")


def test_task_file_reader_is_bounded(tmp_path: Path) -> None:
    task = tmp_path / "task.md"
    task.write_text("x" * 100_001, encoding="utf-8")

    with pytest.raises(ValueError, match="too large"):
        _read_task(f"@{task}")
