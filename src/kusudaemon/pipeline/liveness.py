"""PLAN.md §D0c: a dead run is indistinguishable from a working one.

``phase.json`` reads ``in_progress`` forever once the process that was
making progress dies mid-call (a hung provider call, a killed shell, a
dashboard-hosted thread whose server stopped) — nothing else contradicts
it, so a run that died three days ago and a run genuinely mid-call render
identically in ``status`` and the dashboard. This module is the fix:
``record_driver_start`` writes who is (supposed to be) making progress,
and ``run_liveness`` reads that back against the current phase to tell the
two cases apart.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .run_dir import driver_pid_path, phase_path

# A phase legitimately parked on a human (waiting_for_approval) or already
# terminal (done/error/halted/escalated) is never "stalled" -- only a phase
# that claims to still be actively working can be.
_ACTIVE_STATUSES = {"in_progress"}

# No pid record at all (a run started before this module existed, or one
# whose driver.pid.json write failed) falls back to a pure age check
# against phase.json's own timestamp.
DEFAULT_STALL_AFTER_SECONDS = 600.0


def record_driver_start(run_dir: str | Path) -> None:
    """Best-effort: a failure to write this must never fail a run — it is
    a diagnostic aid, not part of the resume contract."""
    payload = {"pid": os.getpid(), "started_at": time.time(), "host": socket.gethostname()}
    try:
        driver_pid_path(run_dir).write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def _pid_alive(pid: int) -> bool | None:
    """True/False when this host can tell; None when it can't (pid belongs
    to a different host, or the OS refuses even the liveness signal)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but we don't own it -- still alive.
        return True
    except OSError:
        return None
    return True


@dataclass(frozen=True)
class RunLiveness:
    stalled: bool
    reason: str


def run_liveness(
    run_dir: str | Path, *, stall_after_seconds: float = DEFAULT_STALL_AFTER_SECONDS
) -> RunLiveness:
    phase: dict[str, Any] = {}
    try:
        phase = json.loads(phase_path(run_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    status = str(phase.get("status", ""))
    if status not in _ACTIVE_STATUSES:
        return RunLiveness(stalled=False, reason=f"phase status is {status or '(none)'}, not in_progress")

    job: dict[str, Any] = {}
    try:
        job = json.loads(driver_pid_path(run_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        job = {}

    if job.get("host") == socket.gethostname() and isinstance(job.get("pid"), int):
        alive = _pid_alive(job["pid"])
        if alive is False:
            return RunLiveness(
                stalled=True,
                reason=f"driver process pid={job['pid']} is no longer running",
            )
        if alive is True:
            return RunLiveness(stalled=False, reason=f"driver process pid={job['pid']} is alive")

    ts = phase.get("ts")
    if isinstance(ts, (int, float)):
        age = time.time() - ts
        if age > stall_after_seconds:
            return RunLiveness(
                stalled=True,
                reason=f"phase has not advanced in {age:.0f}s (no usable pid record)",
            )
        return RunLiveness(stalled=False, reason=f"phase advanced {age:.0f}s ago")

    return RunLiveness(stalled=False, reason="no pid record and no phase timestamp to judge by")
