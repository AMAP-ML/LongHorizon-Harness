"""PLAN.md §D0c: a dead run is indistinguishable from a working one.

Reproduces the exact durable state a driver process leaves behind when it
dies inside the first model call of a phase (one phase_started event, no
phase_failed, phase.json permanently "in_progress") and checks that
run_liveness can tell that apart from a phase genuinely in flight.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.pipeline.approvals import Approval  # noqa: E402
from kusudaemon.pipeline.approvals import append as append_approval
from kusudaemon.pipeline.liveness import (  # noqa: E402
    DEFAULT_STALL_AFTER_SECONDS,
    HEARTBEAT_STALL_AFTER_SECONDS,
    record_driver_start,
    record_heartbeat,
    run_liveness,
)
from kusudaemon.pipeline.run_dir import phase_path  # noqa: E402


def _write_phase(run_dir: Path, *, status: str, ts: float | None = None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {"phase": "intake", "status": status, "detail": "", "ts": ts if ts is not None else time.time()}
    phase_path(run_dir).write_text(json.dumps(payload), encoding="utf-8")


class RunLivenessTest(unittest.TestCase):
    def test_non_in_progress_phase_is_never_stalled(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            _write_phase(run_dir, status="done")
            record_driver_start(run_dir)
            liveness = run_liveness(run_dir)
        self.assertFalse(liveness.stalled)

    def test_waiting_for_approval_with_pending_approval_is_not_stalled(self) -> None:
        # B2-2: waiting_for_approval is healthy only while there is actually
        # a pending approval to wait on.
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            _write_phase(run_dir, status="waiting_for_approval")
            append_approval(
                run_dir,
                Approval.create("pilot", title="Approve the pilot artifact", allow_input=False),
            )
            record_driver_start(run_dir)
            liveness = run_liveness(run_dir)
        self.assertFalse(liveness.stalled)

    def test_waiting_for_approval_with_no_pending_approval_is_stalled(self) -> None:
        # B2-2: a driver parked on a human with nothing pending is dead by
        # definition — the driver writes the pending record *before* the
        # phase flips, so zero pending means nothing is polling.
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            _write_phase(run_dir, status="waiting_for_approval")
            record_driver_start(run_dir)
            liveness = run_liveness(run_dir)
        self.assertTrue(liveness.stalled)
        self.assertIn("approval", liveness.reason)

    def test_stale_heartbeat_is_stalled_even_with_live_pid(self) -> None:
        # B2-3: a dashboard-hosted driver is a thread inside the always-alive
        # serve process — pid liveness can never detect a dead driver thread;
        # only the heartbeat can.
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            _write_phase(run_dir, status="in_progress")
            record_driver_start(run_dir)  # live pid, heartbeat written now
            record_heartbeat(run_dir)  # ensure fresh
            import socket

            job_path = run_dir / "driver.pid.json"
            payload = json.loads(job_path.read_text(encoding="utf-8"))
            payload["heartbeat_ts"] = time.time() - (HEARTBEAT_STALL_AFTER_SECONDS + 60)
            job_path.write_text(json.dumps(payload), encoding="utf-8")
            liveness = run_liveness(run_dir)
        self.assertTrue(liveness.stalled)
        self.assertIn("heartbeat", liveness.reason)

    def test_fresh_heartbeat_is_not_stalled(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            _write_phase(run_dir, status="in_progress")
            record_driver_start(run_dir)
            liveness = run_liveness(run_dir)
        self.assertFalse(liveness.stalled)

    def test_dead_pid_on_in_progress_phase_is_stalled(self) -> None:
        # A real dead pid: spawn and wait a trivial subprocess so the pid is
        # guaranteed reaped, then record it as the driver's own.
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        dead_pid = proc.pid

        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            _write_phase(run_dir, status="in_progress")
            import socket

            (run_dir / "driver.pid.json").write_text(
                json.dumps({"pid": dead_pid, "started_at": time.time(), "host": socket.gethostname()}),
                encoding="utf-8",
            )
            liveness = run_liveness(run_dir)
        self.assertTrue(liveness.stalled)
        self.assertIn(str(dead_pid), liveness.reason)

    def test_live_pid_on_in_progress_phase_is_not_stalled(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            _write_phase(run_dir, status="in_progress")
            record_driver_start(run_dir)  # records this test process's own pid
            liveness = run_liveness(run_dir)
        self.assertFalse(liveness.stalled)

    def test_no_pid_record_falls_back_to_phase_age(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            old_ts = time.time() - (DEFAULT_STALL_AFTER_SECONDS + 60)
            _write_phase(run_dir, status="in_progress", ts=old_ts)
            liveness = run_liveness(run_dir)
        self.assertTrue(liveness.stalled)

    def test_no_pid_record_and_fresh_phase_is_not_stalled(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            _write_phase(run_dir, status="in_progress", ts=time.time())
            liveness = run_liveness(run_dir)
        self.assertFalse(liveness.stalled)


if __name__ == "__main__":
    unittest.main()
