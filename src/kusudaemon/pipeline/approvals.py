"""Human-in-the-loop approval records, cross-process (PLAN.md §10, §11).

The driver and its human surfaces (the TUI, CLI ``approve``) never share a
lock or a pipe: every approval is a record in the run directory's
``approvals.jsonl``, append-only, latest record per ``approval_id`` wins.
The driver creates a ``pending`` record and then *polls the file* until a
resolved record for the same id shows up; the TUI and the CLI resolve
by appending that resolved record. Any surface can therefore answer what
any other surface asked, and a crashed driver waits on nothing — resolution
is a durable fact on disk, not a pipe buffer. There is no in-memory
authority: the file IS the authority (``tui/state.py`` reads it fresh on
every poll rather than keeping its own mirror).

Record shape (flat dict, JSON-serializable):

    approval_id: str          -- uuid, unique per checkpoint
    kind: str                 -- "intake_question" | "pilot" | "amend" |
                                 "triage" | "reopen" | "confirm"
    title / message           -- what the operator is being asked
    options: [{value, label, style}]  -- button choices (may be empty)
    allow_input / input_label  -- whether a free-form answer is allowed
    context: dict             -- extensible metadata (phase, node_id, ...)
    status: "pending" | "resolved"
    action: str                -- chosen option value once resolved
    user_input: str            -- free-form answer / edited document once resolved
    reason: str | ""
    created_at / resolved_at: float
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .run_dir import approvals_path

_DEFAULT_POLL_INTERVAL = 1.0


@dataclass
class Approval:
    approval_id: str
    kind: str
    title: str
    message: str = ""
    options: list[dict[str, str]] = field(default_factory=list)
    allow_input: bool = True
    input_label: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    action: str = ""
    user_input: str = ""
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    resolved_at: float | None = None

    def resolve(self, *, action: str = "", user_input: str = "", reason: str = "") -> "Approval":
        self.action = str(action or "")
        self.user_input = str(user_input or "")
        self.reason = str(reason or "")
        self.status = "resolved"
        self.resolved_at = time.time()
        return self

    @staticmethod
    def create(
        kind: str, *, title: str, message: str = "", options: list[dict[str, str]] | None = None,
        allow_input: bool = True, input_label: str = "", context: dict[str, Any] | None = None,
    ) -> "Approval":
        return Approval(
            approval_id=uuid.uuid4().hex[:12],
            kind=kind,
            title=title,
            message=message,
            options=list(options or []),
            allow_input=allow_input,
            input_label=input_label,
            context=context or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Approval":
        know = {key: data[key] for key in ("approval_id", "kind", "title") if key in data}
        return Approval(
            **know,
            message=str(data.get("message", "")),
            options=[dict(opt) for opt in list(data.get("options") or [])],
            allow_input=bool(data.get("allow_input", True)),
            input_label=str(data.get("input_label", "")),
            context=dict(data.get("context") or {}),
            status=data.get("status", "pending"),
            action=str(data.get("action", "")),
            user_input=str(data.get("user_input", "")),
            reason=str(data.get("reason", "")),
            created_at=float(data.get("created_at") or time.time()),
            resolved_at=data.get("resolved_at"),
        )


def append(run_dir: str | Path, record: dict[str, Any] | Approval) -> None:
    """Persist one record (pending or resolved). The file is the authority."""
    payload = record.to_dict() if isinstance(record, Approval) else dict(record)
    path = approvals_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()


def read_all(run_dir: str | Path) -> list[Approval]:
    """Latest record per approval_id, in first-seen order."""
    merged: dict[str, Approval] = {}
    order: list[str] = []
    path = approvals_path(run_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or "approval_id" not in data:
            continue
        approval_id = str(data["approval_id"])
        if approval_id not in merged:
            order.append(approval_id)
        merged[approval_id] = Approval.from_dict(data)
    return [merged[key] for key in order]


def pending(run_dir: str | Path) -> list[Approval]:
    return [item for item in read_all(run_dir) if item.status == "pending"]


def find_pending(run_dir: str | Path, *, kind: str, **context: Any) -> Approval | None:
    """Reuse an unanswered checkpoint for the same (kind, context) pair —
    makes resume re-ask the very question it already asked instead of
    stacking duplicates."""
    for item in pending(run_dir):
        if item.kind != kind:
            continue
        matches = all(item.context.get(key) == value for key, value in context.items())
        if matches:
            return item
    return None


def wait_for_resolution(
    run_dir: str | Path,
    approval_id: str,
    *,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    timeout: float | None = None,
) -> Approval:
    """Block the calling (driver) thread until a resolved record for
    ``approval_id`` lands on disk. ``timeout=None`` waits forever — the
    operator is the one control surface that must never be rushed."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        for item in read_all(run_dir):
            if item.approval_id == approval_id and item.status == "resolved":
                return item
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"approval {approval_id} was not resolved in time")
        time.sleep(poll_interval)


class Approver:
    """A polling resolver used by tests and by surface-free automation: a
    background thread that answers every pending approval as soon as it
    lands, so a driver run can be scripted end to end while exercising the
    exact same disk protocol the web UI uses."""

    def __init__(
        self,
        run_dir: str | Path,
        *,
        action: str = "answer",
        user_input: str = "",
        poll_interval: float = 0.05,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.action = action
        self.user_input = user_input
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.resolved: list[str] = []

    def __enter__(self) -> "Approver":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        seen: set[str] = set()
        while not self._stop.is_set():
            for item in pending(self.run_dir):
                if item.approval_id in seen:
                    continue
                seen.add(item.approval_id)
                self.resolve_one(item.approval_id)
            self._stop.wait(self.poll_interval)

    def resolve_one(self, approval_id: str) -> None:
        pending_record = None
        for item in read_all(self.run_dir):
            if item.approval_id == approval_id:
                pending_record = item
        if pending_record is None:
            return
        if pending_record.status == "resolved":
            return
        self.resolve(pending_record)
        self.resolved.append(approval_id)

    def resolve(self, approval: Approval) -> None:
        record = approval.resolve(action=self.action, user_input=self.user_input)
        append(self.run_dir, record)
        self.resolved.append(approval.approval_id)