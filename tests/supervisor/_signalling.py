"""Test helpers for the platform-specific process-group signal.

The supervisor delivers a termination signal to a worker's process group.
On POSIX that is ``os.killpg``; on native Windows it is
``signal_process_group``, because ``os.killpg`` does not exist there and
Windows has no process groups in the POSIX sense.

A test that wants to observe the signal has to patch whichever path the
platform takes. Patching ``os.killpg`` unconditionally fails on Windows,
where the attribute is absent, so ``monkeypatch.setattr`` raises before the
test body runs.

``patch_group_signal`` installs the right patch for the platform and
records the signals that were delivered.
"""

from __future__ import annotations

import signal
import sys
from typing import Any

import pytest


class GroupSignalRecorder:
    """Records the signals the supervisor delivered to a process group."""

    def __init__(self) -> None:
        self.signals: list[Any] = []

    def __call__(self, pid: int, sig: Any, *args: Any, **kwargs: Any) -> bool:
        self.signals.append(sig)
        return True


def patch_group_signal(
    monkeypatch: pytest.MonkeyPatch,
    *,
    recorder: GroupSignalRecorder | None = None,
    raises: BaseException | None = None,
) -> GroupSignalRecorder:
    """Patch the process-group signal path for the current platform.

    ``raises`` makes the patched call raise, which is how the supervisor's
    "the worker is gone" and "permission denied" paths are exercised.

    The recorder captures the signals that were delivered, so a test can
    assert on them the same way on every platform.
    """

    record = recorder if recorder is not None else GroupSignalRecorder()

    def _deliver(pid: int, sig: Any, *args: Any, **kwargs: Any) -> bool:
        if raises is not None:
            raise raises
        return record(pid, sig, *args, **kwargs)

    if sys.platform == "win32":
        # The supervisor calls signal_process_group on Windows.
        monkeypatch.setattr(
            "lh_harness.utils.process_group.signal_process_group",
            _deliver,
            raising=False,
        )
        monkeypatch.setattr(
            "lh_harness.supervisor.service.signal_process_group",
            _deliver,
            raising=False,
        )
    else:
        monkeypatch.setattr(
            "lh_harness.supervisor.service.os.killpg",
            _deliver,
            raising=False,
        )
    return record


def hard_kill_signal() -> int:
    """The signal number the platform uses for an uncatchable kill.

    ``signal.SIGKILL`` exists on POSIX but not on native Windows, where the
    hard kill is a forced ``taskkill /F`` delivered as SIGTERM.
    """

    return getattr(signal, "SIGKILL", signal.SIGTERM)


def expected_hard_kill_name() -> str:
    """The name the platform records for an uncatchable kill.

    ``signal.SIGKILL`` exists on POSIX but not on native Windows, where the
    hard kill is a forced ``taskkill /F`` and the recorded name is SIGTERM.
    """

    return signal.Signals(hard_kill_signal()).name
