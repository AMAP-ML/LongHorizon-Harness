"""Normalize LongHorizon JSONL events and provide replay-safe tailing."""

from __future__ import annotations

import json
import os
import stat
from collections import deque
from pathlib import Path
from typing import Any, Deque

from .models import EventEnvelope
from ..supervisor.control_bus import _open_nofollow as _open_control_nofollow


class EventNormalizationError(ValueError):
    """Raised when a JSONL record cannot be represented by the public schema.

    A single corrupt line must never take down the live dashboard.  The
    tailer catches this exception, records a diagnostic, and continues with
    the remaining records.
    """

EVENT_TYPE_MAP: dict[str, str] = {
    "role_harness_start": "run.started",
    "manager_round_start": "round.manager.started",
    "manager_round_done": "round.manager.completed",
    "executor_role_start": "round.executor.started",
    "executor_role_done": "round.executor.completed",
    "auditor_role_start": "round.auditor.started",
    "auditor_role_done": "round.auditor.completed",
    "auditor_format_repair_start": "round.audit_repair.started",
    "auditor_format_repair_done": "round.audit_repair.completed",
    "final_response_start": "round.final_response.started",
    "final_response_done": "round.final_response.completed",
    "final_response_discarded": "round.final_response.discarded",
    "managed_round_recorded": "round.recorded",
    "role_harness_done": "run.completed",
    "role_harness_cancelled": "run.cancelled",
    "role_harness_failed": "run.failed",
    "approval_created": "operator.approval.pending",
    "approval_resolved": "operator.approval.resolved",
    "instruction_queued": "operator.instruction.queued",
    "instruction_applied": "operator.instruction.applied",
}

_ROUND_KEYS = ("round", "round_index", "round_no", "round_number")
_ROLE_KEYS = ("role", "role_name", "agent_role")

# Event logs are worker-produced input.  A dashboard poll must remain bounded
# even if a worker (or a local process with access to its run directory) writes
# an unexpectedly large JSONL file.  Explicit event ids make retaining a byte
# tail safe: a cursor that fell outside the retained window is reported as a
# resync gap instead of causing an unbounded historical scan.
_MAX_EVENT_LOG_BYTES = 8 * 1024 * 1024
_MAX_EVENT_LINE_BYTES = 512 * 1024
_MAX_EVENT_RECORDS = 20_000


def _as_int(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_type(name: str) -> str:
    if name in EVENT_TYPE_MAP:
        return EVENT_TYPE_MAP[name]
    if name.endswith("_start"):
        return name.removesuffix("_start").replace("_", ".") + ".started"
    if name.endswith("_done"):
        return name.removesuffix("_done").replace("_", ".") + ".completed"
    if name.endswith("_cancelled"):
        return name.removesuffix("_cancelled").replace("_", ".") + ".cancelled"
    return name.replace("_", ".")


def _status_for(event_type: str, raw_name: str) -> str | None:
    if event_type.endswith(".started"):
        return "running"
    if event_type.endswith(".completed") or raw_name.endswith("_done"):
        return "completed"
    if event_type.endswith(".cancelled"):
        return "cancelled"
    if event_type.endswith(".failed"):
        return "failed"
    if event_type.endswith(".pending"):
        return "pending"
    if event_type.endswith(".resolved") or event_type.endswith(".applied"):
        return "completed"
    return None


def normalize_event(
    record: dict[str, Any],
    *,
    run_id: str,
    sequence: int,
    offset: int | None = None,
    fallback_event_id: str | None = None,
) -> EventEnvelope:
    """Convert a legacy Manager event into the versioned public envelope."""

    if not isinstance(record, dict):
        raise EventNormalizationError("event record is not an object")
    raw_schema = record.get("schema_version", 1)
    # Do not coerce arbitrary values (for example ``"bad"``) into a valid
    # protocol version.  It is safer to skip the record and ask the client to
    # resynchronise than to publish a fabricated event.
    if isinstance(raw_schema, bool) or not isinstance(raw_schema, (int, float, str)):
        raise EventNormalizationError("schema_version is not numeric")
    try:
        schema_version = int(raw_schema)
    except (TypeError, ValueError) as exc:
        raise EventNormalizationError("schema_version is not numeric") from exc
    if isinstance(raw_schema, float) and raw_schema != schema_version:
        raise EventNormalizationError("schema_version must be an integer")
    if isinstance(raw_schema, str) and raw_schema.strip() != str(schema_version):
        raise EventNormalizationError("schema_version must be an integer")
    if schema_version < 1 or schema_version > 2:
        raise EventNormalizationError(f"unsupported schema_version: {schema_version}")

    raw_name = str(record.get("event") or record.get("type") or "unknown")
    event_type = str(record.get("type") or _event_type(raw_name))

    # ``events.jsonl`` is produced by a worker and is therefore an untrusted
    # input at the Web/API boundary.  Never let a record identify itself as a
    # different run: doing so would make a run's REST/WS stream a cross-run
    # event oracle, and a foreign terminal event could alter the projection
    # shown to an operator.  Missing run ids remain valid for legacy logs.
    raw_run_id = record.get("run_id")
    if raw_run_id is not None:
        if not isinstance(raw_run_id, str) or raw_run_id != run_id:
            raise EventNormalizationError("event run_id does not match the requested run")

    raw_event_id = record.get("event_id")
    if raw_event_id is None or raw_event_id == "":
        event_id = fallback_event_id or f"{run_id}:{sequence:06d}"
    else:
        if not isinstance(raw_event_id, str):
            raise EventNormalizationError("event_id must be a string")
        # Keep ids bounded before they are copied into snapshots/cursors.  A
        # valid id is still allowed to use a non-numeric suffix for backwards
        # compatibility, but it must be scoped to this run.
        if len(raw_event_id) > 256:
            raise EventNormalizationError("event_id is too long")
        if not raw_event_id.startswith(f"{run_id}:"):
            raise EventNormalizationError("event_id does not belong to the requested run")
        event_id = raw_event_id
    ts_raw = record.get("ts", record.get("timestamp", 0.0))
    try:
        ts = float(ts_raw)
    except (TypeError, ValueError):
        ts = 0.0
    round_number = next((_as_int(record.get(key)) for key in _ROUND_KEYS if record.get(key) is not None), None)
    role = next((str(record.get(key)) for key in _ROLE_KEYS if record.get(key) is not None), None)
    if role is None:
        for candidate in (
            "manager",
            "executor_gui",
            "executor_cli",
            "executor",
            "auditor_format_repair",
            "auditor",
            "final_response",
        ):
            if f".{candidate}." in f".{event_type}." or event_type.startswith(f"{candidate}."):
                role = candidate
                break
    raw_status = record.get("status")
    legacy_episode_status = raw_status if isinstance(raw_status, dict) else None
    if legacy_episode_status is not None:
        # Older Manager builds wrote the complete episode diagnostic object into
        # the event-level ``status`` field.  Keep that diagnostic available to
        # clients, but derive the public lifecycle status from the event type so
        # consumers never receive Python/JSON object repr strings as statuses.
        status = _status_for(event_type, raw_name)
        if status is None:
            nested_status = legacy_episode_status.get("status")
            status = (
                str(nested_status)
                if nested_status is not None
                and not isinstance(nested_status, (dict, list, tuple, set))
                else None
            )
    else:
        status = str(raw_status) if raw_status is not None else _status_for(event_type, raw_name)
    reserved = {
        "schema_version",
        "event_id",
        "type",
        "event",
        "ts",
        "timestamp",
        "run_id",
        "round",
        "round_index",
        "round_no",
        "round_number",
        "role",
        "role_name",
        "agent_role",
        "status",
    }
    payload = {key: value for key, value in record.items() if key not in reserved}
    if legacy_episode_status is not None and "episode_status" not in payload:
        payload["episode_status"] = legacy_episode_status
    legacy = {"event": raw_name}
    if "event_id" in record:
        legacy["event_id"] = record["event_id"]
    return EventEnvelope(
        schema_version=schema_version,
        event_id=event_id,
        type=event_type,
        ts=ts,
        run_id=run_id,
        round=round_number,
        role=role,
        status=status,
        payload=payload,
        legacy=legacy,
        offset=offset,
    )


class EventTailer:
    """Read complete JSONL records while safely ignoring an incomplete tail."""

    def __init__(self, path: str | Path, *, run_id: str = "") -> None:
        self.path = Path(path)
        self.run_id = run_id or "local"
        # Diagnostics describe the most recent read.  They are deliberately
        # kept out of the event stream so a bad line cannot poison consumers.
        self.last_cursor_gap = False
        self.last_resync_required = False
        self.last_warnings: list[str] = []
        self.last_first_event_id: str | None = None
        self.last_last_event_id: str | None = None

    def read(self, *, limit: int = 500, after: str | None = None) -> list[EventEnvelope]:
        self.last_cursor_gap = False
        self.last_resync_required = False
        self.last_warnings = []
        self.last_first_event_id = None
        self.last_last_event_id = None
        try:
            raw, base_offset, head_truncated = self._read_bounded()
        except OSError:
            if after:
                self.last_cursor_gap = True
                self.last_resync_required = True
                self.last_warnings.append("event log is unavailable; a full resync is required")
            return []
        if head_truncated:
            self.last_warnings.append(
                f"event log exceeds {_MAX_EVENT_LOG_BYTES} bytes; replay is limited to the retained tail"
            )
        records: Deque[EventEnvelope] = deque()
        seen_event_ids: set[str] = set()
        records_truncated = False
        offset = base_offset
        sequence = 0
        for line in raw.splitlines(keepends=True):
            line_start = offset
            offset += len(line)
            text = line.strip()
            if not text:
                continue
            if len(line) > _MAX_EVENT_LINE_BYTES:
                self.last_warnings.append(
                    f"ignored oversized event at byte offset {line_start}"
                )
                continue
            try:
                record = json.loads(text.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.last_warnings.append(f"ignored malformed event at byte offset {line_start}")
                continue
            if not isinstance(record, dict):
                self.last_warnings.append(f"ignored non-object event at byte offset {line_start}")
                continue
            sequence += 1
            try:
                # The sequence is based on the complete file, before any
                # tail/limit truncation.  Thus an event keeps the same id in a
                # snapshot, REST replay, and WebSocket reconnect.
                fallback_event_id = (
                    f"{self.run_id}:offset-{line_start:016x}" if head_truncated else None
                )
                item = normalize_event(
                    record,
                    run_id=self.run_id,
                    sequence=sequence,
                    offset=line_start,
                    fallback_event_id=fallback_event_id,
                )
                # A duplicate cursor is ambiguous for replay.  Keep the first
                # occurrence (the one with the smallest file offset) and make
                # the diagnostic visible to clients rather than silently
                # allowing a reconnect to skip or repeat deltas.
                if item.event_id in seen_event_ids:
                    self.last_warnings.append(f"ignored duplicate event_id {item.event_id!r}")
                    continue
                if len(records) >= _MAX_EVENT_RECORDS:
                    removed = records.popleft()
                    seen_event_ids.discard(removed.event_id)
                    records_truncated = True
                records.append(item)
                seen_event_ids.add(item.event_id)
            except EventNormalizationError as exc:
                self.last_warnings.append(f"ignored event {sequence}: {exc}")
                continue
        if records_truncated:
            self.last_warnings.append(
                f"event log contains more than {_MAX_EVENT_RECORDS} records; replay is limited to the retained tail"
            )
        retained_records = list(records)
        replay_from_cursor = False
        if after:
            matching = next((index for index, item in enumerate(retained_records) if item.event_id == after), None)
            if matching is not None:
                retained_records = retained_records[matching + 1 :]
                replay_from_cursor = True
            else:
                # A missing cursor is not equivalent to “start from the
                # beginning”.  It means the client has fallen behind a
                # rotated/truncated log, used another run's cursor, or a
                # corrupt line was encountered.  Tell it to rebuild a
                # snapshot before consuming new deltas.
                self.last_cursor_gap = True
                self.last_resync_required = True
                self.last_warnings.append(f"cursor {after!r} is not present in the retained event log")
        bounded_limit = max(1, min(int(limit), 5000))
        # For a reconnect cursor, ``limit`` bounds the *next* contiguous
        # events.  Applying it to the tail of the whole file would silently
        # skip the first deltas when a run has many events (and makes replay
        # depend on log length).
        bounded = retained_records[:bounded_limit] if replay_from_cursor else retained_records[-bounded_limit:]
        self.last_first_event_id = bounded[0].event_id if bounded else None
        self.last_last_event_id = bounded[-1].event_id if bounded else after
        return bounded

    def _read_bounded(self) -> tuple[bytes, int, bool]:
        """Read at most ``_MAX_EVENT_LOG_BYTES`` from the event log.

        The file descriptor is opened with ``O_NOFOLLOW`` where available so a
        final-path symlink cannot redirect the dashboard to an arbitrary file
        between a path check and the read.  A byte tail starts at a complete
        line; callers get a resync gap if their cursor was discarded.
        """

        # Event logs are worker-produced and may be swapped while a dashboard
        # is polling.  Walk every parent component with anchored no-follow
        # opens and reject hardlink/special-file aliases before reading.
        fd = _open_control_nofollow(self.path)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                return b"", 0, False
            size = metadata.st_size
            if size <= 0:
                return b"", 0, False
            head_truncated = size > _MAX_EVENT_LOG_BYTES
            start = max(0, size - _MAX_EVENT_LOG_BYTES)
            os.lseek(fd, start, os.SEEK_SET)
            raw = os.read(fd, _MAX_EVENT_LOG_BYTES)
        finally:
            os.close(fd)

        if head_truncated:
            # The first bytes may be the middle of a JSON record.  Drop that
            # partial record and report offsets relative to the real file.
            first_newline = raw.find(b"\n")
            if first_newline < 0:
                return b"", size, True
            start += first_newline + 1
            raw = raw[first_newline + 1 :]

        # A worker can be writing the final line while we read.  Never parse
        # that incomplete tail; it will be picked up on the next poll.
        if raw and not raw.endswith((b"\n", b"\r")):
            last_newline = raw.rfind(b"\n")
            raw = raw[: last_newline + 1] if last_newline >= 0 else b""
        return raw, start, head_truncated
