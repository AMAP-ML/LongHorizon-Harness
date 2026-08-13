"""Descriptor-close robustness for control-bus readers.

These tests cover recovery from an *isolated* stolen-descriptor ``EBADF``
(a stray double-close elsewhere in the process recycling the reader's fd
number between open and close).  They are defense in depth around the
root-cause fix for the dashboard walker double-close, not a substitute for
it: only ``EBADF`` is tolerated, every other ``OSError`` must propagate.
"""

import errno
import json
import os

import pytest

from lh_harness.supervisor import control_bus


def _reader_close_harness(monkeypatch, close_error: OSError | None):
    """Route the reader's own close through ``close_error``, keep others real."""

    real_open = control_bus._open_nofollow
    real_close = os.close
    reader_fd: dict[str, int] = {}
    failed: list[int] = []

    def tracking_open(target):
        fd = real_open(target)
        reader_fd["fd"] = fd
        return fd

    def failing_close(fd: int) -> None:
        real_close(fd)
        if fd == reader_fd.get("fd") and close_error is not None:
            failed.append(fd)
            raise close_error

    monkeypatch.setattr(control_bus, "_open_nofollow", tracking_open)
    monkeypatch.setattr(os, "close", failing_close)
    return failed


def test_read_json_file_survives_ebadf_on_close(tmp_path, monkeypatch):
    """An isolated stolen-descriptor EBADF must cost one close, not the poller.

    The watcher task that calls this helper runs for the whole run, so the
    failure has to degrade gracefully instead of storing an exception that
    resurfaces at shutdown as the process exit status.
    """

    payload = {"requested_action": "stop"}
    path = tmp_path / "status.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    failed = _reader_close_harness(
        monkeypatch, OSError(errno.EBADF, "Bad file descriptor")
    )
    try:
        result = control_bus._read_json_file(path)
    finally:
        monkeypatch.undo()

    assert failed, "the reader must still attempt to close its descriptor"
    assert result == payload


def test_read_json_file_propagates_non_ebadf_close_errors(tmp_path, monkeypatch):
    """Only EBADF is recoverable noise; a real I/O failure must surface.

    Swallowing e.g. EIO here would silently turn permission or filesystem
    failures into an empty control status and legitimate stop/abort requests
    could be ignored for the rest of the run.
    """

    path = tmp_path / "status.json"
    path.write_text(json.dumps({"requested_action": "stop"}), encoding="utf-8")

    failed = _reader_close_harness(
        monkeypatch, OSError(errno.EIO, "Input/output error")
    )
    try:
        with pytest.raises(OSError) as excinfo:
            control_bus._read_json_file(path)
    finally:
        monkeypatch.undo()

    assert failed
    assert excinfo.value.errno == errno.EIO


def test_read_jsonl_propagates_non_ebadf_close_errors(tmp_path, monkeypatch):
    path = tmp_path / "control.jsonl"
    path.write_text(json.dumps({"event": "noop"}) + "\n", encoding="utf-8")

    failed = _reader_close_harness(
        monkeypatch, OSError(errno.EIO, "Input/output error")
    )
    try:
        with pytest.raises(OSError) as excinfo:
            control_bus._read_jsonl(path)
    finally:
        monkeypatch.undo()

    assert failed
    assert excinfo.value.errno == errno.EIO


def test_read_jsonl_survives_ebadf_on_close(tmp_path, monkeypatch):
    record = {"event": "noop"}
    path = tmp_path / "control.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    failed = _reader_close_harness(
        monkeypatch, OSError(errno.EBADF, "Bad file descriptor")
    )
    try:
        result = control_bus._read_jsonl(path)
    finally:
        monkeypatch.undo()

    assert failed
    assert result == [record]
