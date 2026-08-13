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
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .approvals import pending as pending_approvals
from .run_dir import driver_pid_path, phase_path

# A phase legitimately parked on a human (waiting_for_approval) or already
# terminal (done/error/halted/escalated) is never "stalled" -- only a phase
# that claims to still be actively working can be.
#
# B2-2 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): waiting_for_approval is an
# *active* status — a driver parked on a human is fine only while there is
# actually a pending approval for that human to answer. The instant the
# operator's answer is on disk (or the approval was never created), a
# `waiting_for_approval` phase with zero pending approvals is a stalled run
# by definition: nothing is polling approvals.jsonl.
_ACTIVE_STATUSES = {"in_progress", "waiting_for_approval"}

# No pid record at all (a run started before this module existed, or one
# whose driver.pid.json write failed) falls back to a pure age check
# against phase.json's own timestamp.
DEFAULT_STALL_AFTER_SECONDS = 600.0

# B2-3: heartbeat staleness threshold. A driver thread that stops refreshing
# driver.pid.json's heartbeat_ts for this long is treated as stalled
# regardless of pid — the dashboard hosts drivers as threads inside the
# long-lived `serve` process, so pid liveness structurally cannot detect a
# dead driver thread.
HEARTBEAT_STALL_AFTER_SECONDS = 30.0
HEARTBEAT_INTERVAL_SECONDS = 5.0


def record_driver_start(run_dir: str | Path) -> None:
    """Best-effort: a failure to write this must never fail a run — it is
    a diagnostic aid, not part of the resume contract.

    B2-3: writes ``thread_ident`` (the driver thread, not the pid — for a
    dashboard-hosted run the pid is the long-lived serve process and is
    always alive) and a fresh ``heartbeat_ts``."""
    payload = {
        "pid": os.getpid(),
        "thread_ident": threading.get_ident(),
        "started_at": time.time(),
        "host": socket.gethostname(),
        "heartbeat_ts": time.time(),
    }
    try:
        driver_pid_path(run_dir).write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def record_heartbeat(run_dir: str | Path) -> None:
    """Refresh ``heartbeat_ts`` in driver.pid.json without touching the rest
    of the record. Best-effort, same as ``record_driver_start``."""
    path = driver_pid_path(run_dir)
    payload: dict[str, Any] = {}
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    payload["heartbeat_ts"] = time.time()
    try:
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


def start_heartbeat_thread(run_dir: str | Path) -> "_HeartbeatHandle":
    """B2-3: a daemon thread that refreshes ``heartbeat_ts`` every
    ``HEARTBEAT_INTERVAL_SECONDS`` while the *calling* (driver) thread is
    alive, then stops itself. The heartbeat stops with the driver thread —
    the one signal that works for both a CLI driver (own process: pid dies
    too) and a dashboard-hosted driver (a thread inside the always-alive
    serve process). The driver's ``run()`` finally-block calls ``.stop()``
    so a completed run doesn't keep heartbeating forever."""
    owner = threading.current_thread()
    loop_stop = threading.Event()
    stop_flag = threading.Event()

    def _loop() -> None:
        while not loop_stop.is_set():
            if not owner.is_alive():
                break
            record_heartbeat(run_dir)
            loop_stop.wait(HEARTBEAT_INTERVAL_SECONDS)
        stop_flag.set()

    thread = threading.Thread(target=_loop, name="kusudaemon-heartbeat", daemon=True)
    thread.start()
    return _HeartbeatHandle(thread, loop_stop, stop_flag)


class _HeartbeatHandle:
    def __init__(
        self, thread: threading.Thread, loop_stop: threading.Event, stop_flag: threading.Event
    ) -> None:
        self._thread = thread
        self._loop_stop = loop_stop
        self._stop_flag = stop_flag

    def stop(self) -> None:
        self._loop_stop.set()
        self._thread.join(timeout=2)


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
        return RunLiveness(stalled=False, reason=f"phase status is {status or '(none)'}, not in {_ACTIVE_STATUSES}")

    # B2-2: a waiting_for_approval phase is only healthy while there is
    # actually a pending approval to wait on. The driver writes the pending
    # record *before* flipping the phase, so a real wait always has one —
    # zero pending means the driver that created the phase is gone.
    if status == "waiting_for_approval":
        if pending_approvals(run_dir):
            return RunLiveness(
                stalled=False,
                reason=f"{len(pending_approvals(run_dir))} approval(s) pending, driver waiting",
            )
        return RunLiveness(
            stalled=True,
            reason="phase is waiting_for_approval but no approval is pending — nothing is polling approvals.jsonl",
        )

    job: dict[str, Any] = {}
    try:
        job = json.loads(driver_pid_path(run_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        job = {}

    # B2-3: the heartbeat is the primary liveness signal. A driver thread
    # that has stopped refreshing it is dead, regardless of pid — the pid
    # for a dashboard-hosted run is the long-lived serve process, which is
    # always alive.
    heartbeat_ts = job.get("heartbeat_ts")
    if isinstance(heartbeat_ts, (int, float)):
        age = time.time() - heartbeat_ts
        if age > HEARTBEAT_STALL_AFTER_SECONDS:
            return RunLiveness(
                stalled=True,
                reason=f"driver heartbeat has not advanced in {age:.0f}s",
            )
        return RunLiveness(stalled=False, reason=f"driver heartbeat {age:.0f}s ago")

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
