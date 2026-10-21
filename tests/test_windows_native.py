"""Native Windows boundary and lifecycle tests, without an agent subscription."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='requires native Windows')


def test_private_file_checks_the_writable_handle_before_truncation(tmp_path):
    from lh_harness.supervisor.control_bus import _ensure_dir_fd_nofollow, _open_private_regular_at
    outside = tmp_path / 'outside'
    outside.write_bytes(b'keep me')
    directory = tmp_path / 'run'
    directory.mkdir()
    os.link(outside, directory / 'alias')
    fd = _ensure_dir_fd_nofollow(directory)
    try:
        with pytest.raises(OSError):
            _open_private_regular_at(fd, 'alias', os.O_RDWR | os.O_TRUNC)
        assert outside.read_bytes() == b'keep me'
        private = _open_private_regular_at(fd, 'private', os.O_RDWR)
        os.write(private, b'old data')
        os.close(private)
        private = _open_private_regular_at(fd, 'private', os.O_RDWR | os.O_TRUNC)
        try:
            assert os.fstat(private).st_size == 0
        finally:
            os.close(private)
    finally:
        os.close(fd)


def test_directory_walk_rejects_intermediate_junction(tmp_path):
    from lh_harness.supervisor.control_bus import _ensure_dir_fd_nofollow
    outside = tmp_path / 'outside'
    outside.mkdir()
    junction = tmp_path / 'link'
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)], check=True, capture_output=True)
    with pytest.raises(OSError):
        _ensure_dir_fd_nofollow(junction / 'new')
    assert not (outside / 'new').exists()


def test_empty_lock_and_event_appends_do_not_add_placeholder_bytes(tmp_path):
    from lh_harness.utils.file_lock import lock_handle
    from lh_harness.manager import _append_event
    with (tmp_path / 'lock').open('a+b') as handle:
        with lock_handle(handle):
            assert handle.tell() == 0
    assert (tmp_path / 'lock').read_bytes() == b''
    events = tmp_path / 'run' / 'logs' / 'role_orchestration' / 'events.jsonl'
    _append_event(events, 'role_harness_start', {})
    _append_event(events, 'role_harness_done', {})
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert [r['event_id'] for r in rows] == ['run:000001', 'run:000002']


def test_supervisor_create_status_restart_abort_and_continue(tmp_path, monkeypatch):
    from lh_harness.supervisor.service import RunSupervisor
    from lh_harness.supervisor.control_bus import ControlBus
    # Real subprocesses exercise handles and taskkill, but never invoke an agent.
    monkeypatch.setattr(RunSupervisor, '_worker_command', lambda *a, **kw: [sys.executable, '-u', '-c', 'import time; time.sleep(120)'])
    supervisor = RunSupervisor(tmp_path / 'runs', workspace_root=tmp_path / 'workspace')
    run = supervisor.create_run(task='native lifecycle', max_rounds=2)
    run_id = run['id']
    process = supervisor._processes[run_id]
    try:
        assert supervisor.status(run_id)['alive']
        # A restarted supervisor lacks the Popen handle: probing must not kill it.
        restarted = RunSupervisor(tmp_path / 'runs', workspace_root=tmp_path / 'workspace')
        assert restarted._is_alive(run_id)
        assert process.poll() is None
        bus = ControlBus(tmp_path / 'runs' / run_id)
        bus.append('inject_instruction', {'text': 'feedback'}, created_by='test')
        supervisor.abort(run_id)
        process.wait(timeout=15)
        assert not supervisor.status(run_id)['alive']
        resumed = supervisor.resume(run_id, mode='continue')
        assert resumed['id'] == run_id
        process = supervisor._processes[run_id]
        assert supervisor.status(run_id)['alive']
        supervisor.abort(run_id)
        process.wait(timeout=15)
        assert not supervisor.status(run_id)['alive']
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=15)
