"""The liveness probe must observe, never act.

`os.kill(pid, 0)` is the POSIX idiom for "does this process exist" -- and a
kill switch on Windows, where os.kill can only deliver console control events
and terminates the target for every other signal value, zero included. The
first CI run on a Windows machine with admin rights demonstrated the failure
mode: a supervisor status poll probing a fake test pid terminated an unrelated
live process on the runner and took the whole pytest process down with it.
"""

from __future__ import annotations

import subprocess
import sys
import time

from lh_harness.utils.process_group import process_alive


def test_probe_sees_a_live_process_and_does_not_kill_it():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert process_alive(child.pid) is True
        # The probe is the whole test: after it, the child must still be
        # running. The old Windows implementation terminated it right here.
        time.sleep(0.3)
        assert child.poll() is None
        assert process_alive(child.pid) is True
    finally:
        child.kill()
        child.wait(timeout=10)


def test_probe_sees_a_dead_process():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    # PID reuse cannot strike in the instant between wait() returning and the
    # probe; Windows keeps the pid reserved while the handle is open.
    assert process_alive(child.pid) is False


def test_probe_rejects_nonsense_pids():
    # pid 0 is our own process group on POSIX and the idle process on Windows;
    # negative pids are POSIX group addresses. None can be a tracked worker.
    assert process_alive(0) is False
    assert process_alive(-1) is False
