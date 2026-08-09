"""Shared Web state: reads harness logs and holds human-approval records.

The harness imports this state and the optional approval hook. The FastAPI Web
server projects the same state into its public snapshot and control APIs.

The state object is shared between two worlds:

* the async management loop (via the round hook / approval gate), and
* the HTTP server thread (serving the Web UI + JSON API).

On-disk logs are always read fresh from ``log_dir`` so the UI reflects live
progress. Approval records are kept in memory and guarded by a lock.
"""

from __future__ import annotations

import json
import os
import re
import stat as stat_module
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..types import DEFAULT_LOG_DIR
from ..supervisor.control_bus import ControlBus, _ensure_dir_fd_nofollow, _open_private_regular_at
from ..supervisor.lifecycle import TERMINAL_STATUSES, canonical_lifecycle_status
from ..utils.run_boundary import safe_run_dir, safe_run_logs, safe_run_role, safe_run_rounds

from ..agent_logs import parse_trajectory as parse_agent_trajectory
from ..role_prompts import parse_role_manager_next_step

# Roles whose raw trajectory a round directory can hold.
_TRAJECTORY_ROLES = (
    "manager",
    "executor",
    "auditor",
    "auditor_format_repair",
    "final_response",
)


_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_MAX_TRAJECTORY_BYTES = 16 * 1024 * 1024
_MAX_TRAJECTORY_STEPS = 5_000
_MAX_ROUND_TEXT_BYTES = 512 * 1024
_MAX_JSON_BYTES = 8 * 1024 * 1024
_MAX_JSONL_BYTES = 8 * 1024 * 1024
_MAX_JSONL_LINE_BYTES = 512 * 1024
_MAX_JSONL_RECORDS = 20_000
_MAX_ARTIFACT_COUNT = 512
_MAX_ARTIFACT_SCAN = 2_048
_MAX_ARTIFACT_NAME_CHARS = 256
_MAX_ROUND_COUNT = 10_000
_MAX_ROUND_INDEX = 1_000_000


@dataclass
class ApprovalOption:
    """One selectable answer shown as a button in the approval dialog."""

    value: str            # machine value returned as the response action
    label: str            # button text shown to the operator
    style: str = ""       # optional visual hint: "primary" | "danger" | ""

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "label": self.label, "style": self.style}


@dataclass
class Approval:
    """A single human-in-the-loop checkpoint (a question with options).

    Request fields (shown in the dialog): ``title``, ``message``, ``options``,
    ``allow_input`` / ``input_label``. Response fields (filled on resolve):
    ``action`` (the chosen option value), ``reason`` (optional why), and
    ``user_input`` (free-form text). ``context`` carries extensible metadata
    (e.g. ``phase``, ``kind``, ``round_index``) without widening the schema.
    """

    approval_id: str
    title: str
    message: str = ""
    options: list[ApprovalOption] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)  # quick-answer choices (e.g. 是/否)
    allow_input: bool = True
    input_label: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending | resolved
    # --- response ---
    action: str = ""
    reason: str = ""
    user_input: str = ""
    created_at: float = field(default_factory=time.time)
    resolved_at: float | None = None

    @property
    def round_index(self) -> int:
        return int(self.context.get("round_index", 0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "title": self.title,
            "message": self.message,
            "options": [opt.to_dict() for opt in self.options],
            "answers": list(self.answers),
            "allow_input": self.allow_input,
            "input_label": self.input_label,
            "context": self.context,
            "round_index": self.round_index,
            "status": self.status,
            "action": self.action,
            "reason": self.reason,
            "user_input": self.user_input,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }


class DashboardState:
    """Thread-safe store bridging the management loop and the web server."""

    def __init__(
        self,
        log_dir: str | Path | None = None,
        *,
        task: str = "",
        runs_root: str | Path | None = None,
        control_enabled: bool = False,
    ) -> None:
        # When only a runs_root is given (manual dashboard browsing), the newest
        # run is auto-selected so the UI shows something immediately; the user
        # can switch to any other run from the UI.
        self.runs_root = Path(runs_root).expanduser().resolve() if runs_root else None
        resolved = Path(log_dir) if log_dir else None
        if resolved is None and self.runs_root is not None:
            runs = self._scan_runs()
            if runs:
                resolved = Path(runs[0]["log_dir"])
        self.log_dir = (resolved or Path(DEFAULT_LOG_DIR)).expanduser().resolve(strict=False)
        self.task = task
        self.control_enabled = control_enabled
        self.control_bus = ControlBus(self.log_dir.parent)
        self._lock = threading.Lock()
        self._approvals: dict[str, Approval] = {}
        self._approval_order: list[str] = []
        # Free-form operator notes to inject into upcoming manager prompts,
        # independent of a blocking approval checkpoint.
        self._pending_injections: list[str] = []

    # ------------------------------------------------------------------
    # Run selection (manual dashboard browsing across per-run result folders)
    # ------------------------------------------------------------------
    def _scan_runs(self) -> list[dict[str, Any]]:
        root = self.runs_root
        if root is None:
            return []
        try:
            if not root.is_dir():
                return []
        except OSError:
            return []
        runs: list[dict[str, Any]] = []
        try:
            entries = sorted(root.iterdir())
        except OSError:
            return runs
        for entry in entries:
            run_dir = safe_run_dir(root, entry.name)
            if run_dir is None or not run_dir.is_dir():
                continue
            # New runs use lh_harness/role_orchestration; the boundary helper
            # safe_run_role also accepts the legacy Dashboard layout.
            log_dir = safe_run_logs(
                root,
                run_dir,
                require_role_management=True,
                allow_missing=False,
            )
            if log_dir is None:
                continue
            if safe_run_rounds(root, run_dir, allow_missing=True) is None:
                continue
            role_dir = safe_run_role(root, run_dir, allow_missing=False)
            if role_dir is None:
                continue
            try:
                mtime = role_dir.stat().st_mtime
            except OSError:
                mtime = 0.0
            report = _read_json(log_dir / "report.json", strict_parent=True)
            status = report.get("status") if isinstance(report, dict) else None
            runs.append(
                {
                    "id": entry.name,
                    "log_dir": str(log_dir),
                    "mtime": mtime,
                    "status": status or "",
                }
            )
        runs.sort(key=lambda item: item["mtime"], reverse=True)
        return runs

    def list_runs(self) -> list[dict[str, Any]]:
        return self._scan_runs()

    def select_run(self, run_id: str) -> bool:
        root = self.runs_root
        if (
            root is None
            or not isinstance(run_id, str)
            or not run_id
            or len(run_id) > 128
            or "/" in run_id
            or "\\" in run_id
            or run_id in {".", ".."}
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in run_id)
        ):
            return False
        run_dir = safe_run_dir(root, run_id)
        if run_dir is None or not run_dir.is_dir():
            return False
        log_dir = safe_run_logs(
            root,
            run_dir,
            require_role_management=True,
            allow_missing=False,
        )
        if log_dir is None:
            return False
        if safe_run_rounds(root, run_dir, allow_missing=True) is None:
            return False
        with self._lock:
            self.log_dir = log_dir
            self.task = ""  # will be re-read from the selected run's report
        return True

    @property
    def current_run_id(self) -> str:
        if self.runs_root is None:
            return ""
        try:
            return self.log_dir.parent.relative_to(self.runs_root.resolve()).parts[0]
        except (ValueError, IndexError):
            return self.log_dir.parent.name

    # ------------------------------------------------------------------
    # On-disk log reading (always fresh so the UI tracks live progress)
    # ------------------------------------------------------------------
    @property
    def _role_dir(self) -> Path:
        canonical = self.log_dir / "role_orchestration"
        legacy = self.log_dir / "role_management"
        try:
            if canonical.exists() or canonical.is_symlink():
                return canonical
            if legacy.exists() or legacy.is_symlink():
                return legacy
        except OSError:
            pass
        # New/custom log directories always use the canonical layout. The
        # legacy path is selected only when it actually exists.
        return canonical

    @property
    def role_dir(self) -> Path:
        """Canonical role ledger path used by REST/WebSocket projections."""

        return self._role_dir

    def _safe_round_dir(self, round_index: int) -> Path | None:
        """Resolve one round directory without following a root escape symlink."""

        if (
            isinstance(round_index, bool)
            or not isinstance(round_index, int)
            or round_index < 0
            or round_index > _MAX_ROUND_INDEX
        ):
            return None
        rounds_root = self._role_dir / "rounds"
        candidate_round_dir = rounds_root / f"round_{round_index:03d}"
        try:
            # Do not accept symlinked directory components.  Resolving and
            # checking a path, then opening it later, leaves a replacement race
            # that can redirect an artifact read outside this run.
            if self._role_dir.is_symlink() or rounds_root.is_symlink() or candidate_round_dir.is_symlink():
                return None
            resolved_role_dir = self._role_dir.resolve()
            resolved_rounds_root = rounds_root.resolve()
            if self.runs_root is not None:
                resolved_role_dir.relative_to(self.runs_root)
            resolved_rounds_root.relative_to(resolved_role_dir)
            resolved_round_dir = (resolved_rounds_root / f"round_{round_index:03d}").resolve()
            resolved_round_dir.relative_to(resolved_rounds_root)
            if self.runs_root is not None:
                resolved_round_dir.relative_to(self.runs_root)
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved_round_dir if resolved_round_dir.is_dir() else None

    def read_report(self) -> dict[str, Any]:
        for candidate in (self.log_dir / "report.json", self._role_dir / "report.json"):
            data = _read_json(candidate, strict_parent=True)
            if isinstance(data, dict):
                return data
        return {}

    def read_rounds(self) -> list[dict[str, Any]]:
        # rounds.jsonl / report.json are only written when a round *finishes*.
        # To reflect live progress we also scan the per-round directories, whose
        # artifact files (manager_plan.txt, executor_output.txt, ...) are written
        # incrementally while a round is still running.
        report = self.read_report()
        events = _read_jsonl(self._role_dir / "events.jsonl", strict_parent=True)
        recorded: dict[int, dict[str, Any]] = {}
        for item in _read_jsonl(self._role_dir / "rounds.jsonl", strict_parent=True):
            index = item.get("round_index")
            if isinstance(index, int):
                recorded[index] = item  # append-only ledger: keep the latest
        if not recorded:
            report_rounds = report.get("rounds")
            if isinstance(report_rounds, list):
                for item in report_rounds:
                    index = item.get("round_index") if isinstance(item, dict) else None
                    if isinstance(index, int):
                        recorded[index] = item

        report_status = canonical_lifecycle_status(report.get("status")) if report.get("status") else ""
        run_finished = report_status in TERMINAL_STATUSES or any(
            event.get("event") in {"role_harness_cancelled", "role_harness_done"}
            for event in events
        )
        merged: dict[int, dict[str, Any]] = {}
        for index, live in self._scan_round_dirs().items():
            live["in_progress"] = not run_finished and index not in recorded
            merged[index] = {**live, **recorded.get(index, {})}
            merged[index]["in_progress"] = live["in_progress"]
        for index, item in recorded.items():
            if index not in merged:
                merged[index] = {**item, "in_progress": False}
        for index, item in merged.items():
            active_role, lifecycle_seen = _active_role_for_round(events, index)
            if lifecycle_seen:
                item["active_role"] = active_role
        return [merged[key] for key in sorted(merged)]

    def _scan_round_dirs(self) -> dict[int, dict[str, Any]]:
        rounds_root = self._safe_rounds_root()
        if rounds_root is None:
            return {}
        result: dict[int, dict[str, Any]] = {}
        if not rounds_root.is_dir():
            return result
        try:
            entries = rounds_root.iterdir()
            for entry_number, entry in enumerate(entries):
                if entry_number >= _MAX_ROUND_COUNT:
                    break
                if entry.is_symlink() or not entry.is_dir():
                    continue
                try:
                    resolved_entry = entry.resolve()
                    resolved_entry.relative_to(rounds_root)
                except (OSError, RuntimeError, ValueError):
                    continue
                match = re.match(r"round_(\d+)$", entry.name)
                if not match:
                    continue
                index = int(match.group(1))
                if index > _MAX_ROUND_INDEX:
                    continue
                result[index] = self._round_from_dir(index, resolved_entry)
        except OSError:
            return result
        return result

    def _safe_rounds_root(self) -> Path | None:
        rounds_root = self._role_dir / "rounds"
        try:
            if self._role_dir.is_symlink() or rounds_root.is_symlink():
                return None
            resolved_role_dir = self._role_dir.resolve()
            resolved_rounds_root = rounds_root.resolve()
            if self.runs_root is not None:
                resolved_role_dir.relative_to(self.runs_root)
            resolved_rounds_root.relative_to(resolved_role_dir)
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved_rounds_root

    def _round_from_dir(self, index: int, round_dir: Path) -> dict[str, Any]:
        def _text(name: str) -> str:
            value, truncated = _read_text_bounded(
                round_dir / name,
                _MAX_ROUND_TEXT_BYTES,
                tail=False,
                strict_parent=True,
            )
            if value is None:
                return ""
            if truncated:
                return value + "\n[…truncated…]"
            return value

        def _status(role: str) -> dict[str, Any]:
            data = _read_json(round_dir / f"{role}_metadata.json", strict_parent=True)
            if not isinstance(data, dict):
                return {}
            return {
                key: data.get(key)
                for key in ("status", "error", "duration_ms")
                if data.get(key) is not None
            }

        plan_text = _text("manager_plan.txt")
        final_response = _text("final_response.txt")
        auditor_status = _status("auditor")
        repair_status = _status("auditor_format_repair")
        if repair_status:
            auditor_status = {
                **auditor_status,
                "format_repair_attempted": True,
                "format_repair_status": repair_status,
            }
        return {
            "round_index": index,
            "next_step": _infer_next_step(plan_text),
            # Written by the loop before the executor starts, so a round still in
            # progress already shows which tier is running.
            "executor_tier": _text("executor_tier.txt").strip(),
            "plan_text": plan_text,
            "task_state": _text("task_state.txt"),
            "task_contract": _text("task_contract.txt"),
            "executor_output": _text("executor_output.txt"),
            "auditor_report": _text("auditor_report.txt"),
            "harness_feedback": _text("harness_feedback.txt"),
            "final_response": final_response,
            "related_report_refs": [],
            "manager_status": _status("manager"),
            "executor_status": _status("executor"),
            "auditor_status": auditor_status,
            "final_response_status": _status("final_response"),
            "in_progress": True,
        }

    def read_events(self, *, limit: int = 500) -> list[dict[str, Any]]:
        events = _read_jsonl(self._role_dir / "events.jsonl", strict_parent=True)
        bounded_limit = max(1, min(int(limit), 5_000))
        return events[-bounded_limit:]

    def list_round_artifacts(self, round_index: int) -> list[str]:
        round_dir = self._safe_round_dir(round_index)
        if round_dir is None:
            return []
        try:
            fd = _open_nofollow(round_dir, directory=True, strict_parent=True)
        except OSError:
            return []
        artifacts: list[str] = []
        try:
            with os.scandir(fd) as entries:
                for entry_number, entry in enumerate(entries):
                    if entry_number >= _MAX_ARTIFACT_SCAN:
                        break
                    name = str(entry.name)
                    if len(name) > _MAX_ARTIFACT_NAME_CHARS:
                        continue
                    candidate = round_dir / name
                    try:
                        child_fd = _open_nofollow(candidate, strict_parent=True)
                        try:
                            mode = os.fstat(child_fd).st_mode
                        finally:
                            os.close(child_fd)
                    except OSError:
                        continue
                    if stat_module.S_ISREG(mode):
                        artifacts.append(name)
                    if len(artifacts) >= _MAX_ARTIFACT_COUNT:
                        break
        except OSError:
            return []
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        return sorted(artifacts)

    def resolve_round_artifact(self, round_index: int, name: str) -> Path | None:
        # Guard against path traversal: only allow plain file names.
        if not isinstance(name, str) or len(name) > _MAX_ARTIFACT_NAME_CHARS:
            return None
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
            return None
        if "/" in name or "\\" in name or name in {"", ".", ".."}:
            return None
        rounds_root = self._safe_rounds_root()
        round_dir = self._safe_round_dir(round_index)
        if rounds_root is None or round_dir is None:
            return None
        try:
            # Resolve the directory chain itself before resolving the file.
            # Checking only ``target.relative_to(round_dir.resolve())`` lets a
            # symlinked ``round_001`` redirect the API into an arbitrary
            # directory (for example a workspace secret) while still passing
            # the filename traversal check.
            resolved_round_dir = round_dir
            candidate = round_dir / name
            if candidate.is_symlink():
                return None
            target = candidate.resolve()
            target.relative_to(resolved_round_dir)
        except (OSError, RuntimeError, ValueError):
            return None
        # Reject a final symlink as well as a symlinked round directory.  The
        # read methods below open the returned path with O_NOFOLLOW, so a
        # replacement after this check cannot turn into a path escape.
        if target.is_symlink() or not target.is_file():
            return None
        return target

    def read_round_artifact(self, round_index: int, name: str) -> str | None:
        target = self.resolve_round_artifact(round_index, name)
        if target is None:
            return None
        data, too_large = _read_file_bounded(target, _MAX_ARTIFACT_BYTES, tail=False, strict_parent=True)
        if too_large or data is None:
            return None
        return data.decode("utf-8", errors="replace")

    def read_round_artifact_bytes(self, round_index: int, name: str) -> bytes | None:
        target = self.resolve_round_artifact(round_index, name)
        if target is None:
            return None
        data, too_large = _read_file_bounded(target, _MAX_ARTIFACT_BYTES, tail=False, strict_parent=True)
        if too_large:
            return None
        if data is None:
            return None
        return data

    def round_artifact_size(self, round_index: int, name: str) -> int | None:
        """Return a securely-opened artifact size, or ``None`` if unavailable."""

        target = self.resolve_round_artifact(round_index, name)
        if target is None:
            return None
        try:
            fd = _open_nofollow(target, strict_parent=True)
            try:
                metadata = os.fstat(fd)
            finally:
                os.close(fd)
        except OSError:
            return None
        return int(metadata.st_size) if stat_module.S_ISREG(metadata.st_mode) else None

    # ------------------------------------------------------------------
    # Full role trajectories normalized across supported agent backends
    # ------------------------------------------------------------------
    def list_round_roles(self, round_index: int) -> list[str]:
        """Roles that have a saved raw trajectory for the given round."""
        round_dir = self._safe_round_dir(round_index)
        if round_dir is None:
            return []
        roles: list[str] = []
        for role in _TRAJECTORY_ROLES:
            if self._trajectory_path(round_dir, role) is not None:
                roles.append(role)
        return roles

    @staticmethod
    def _trajectory_path(round_dir: Path, role: str) -> Path | None:
        # New runs save .jsonl; keep reading .txt for older runs.
        for suffix in (".jsonl", ".txt"):
            candidate = round_dir / f"{role}_raw_trajectory{suffix}"
            try:
                if candidate.is_symlink():
                    continue
                resolved = candidate.resolve()
                resolved.relative_to(round_dir)
            except (OSError, RuntimeError, ValueError):
                continue
            try:
                fd = _open_nofollow(resolved, strict_parent=True)
                try:
                    is_file = stat_module.S_ISREG(os.fstat(fd).st_mode)
                finally:
                    os.close(fd)
            except OSError:
                continue
            if is_file:
                return resolved
        return None

    @staticmethod
    def _normalized_trajectory_path(round_dir: Path, role: str) -> Path | None:
        candidate = round_dir / f"{role}_trajectory.jsonl"
        try:
            if candidate.is_symlink():
                return None
            resolved = candidate.resolve()
            resolved.relative_to(round_dir)
            fd = _open_nofollow(resolved, strict_parent=True)
            try:
                is_file = stat_module.S_ISREG(os.fstat(fd).st_mode)
            finally:
                os.close(fd)
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved if is_file else None

    def _round_trajectory_sizes(self, round_index: int) -> dict[str, int]:
        """Current byte size of each role's trajectory file (for live refresh)."""
        round_dir = self._safe_round_dir(round_index)
        sizes: dict[str, int] = {}
        if round_dir is None:
            return sizes
        for role in _TRAJECTORY_ROLES:
            path = self._trajectory_path(round_dir, role)
            if path is not None:
                try:
                    fd = _open_nofollow(path, strict_parent=True)
                    try:
                        sizes[role] = os.fstat(fd).st_size
                    finally:
                        os.close(fd)
                except OSError:
                    sizes[role] = 0
        return sizes

    def read_trajectory(self, round_index: int, role: str) -> dict[str, Any] | None:
        """Parse a role's raw agent trajectory into ordered, backend-agnostic steps."""
        if role not in _TRAJECTORY_ROLES:
            return None
        round_dir = self._safe_round_dir(round_index)
        if round_dir is None:
            return None
        normalized_path = self._normalized_trajectory_path(round_dir, role)
        traj_path = normalized_path or self._trajectory_path(round_dir, role)
        if traj_path is None:
            return None
        data, too_large = _read_file_bounded(traj_path, _MAX_TRAJECTORY_BYTES, tail=False, strict_parent=True)
        if too_large:
            return {
                "round_index": round_index,
                "role": role,
                "steps": [],
                "step_count": 0,
                "raw_chars": 0,
                "warning": "trajectory is too large to render",
            }
        if data is None:
            return None
        raw = data.decode("utf-8", errors="replace")
        # The byte cap alone does not bound response/DOM growth: a valid JSONL
        # file can contain hundreds of thousands of tiny events.  Ask the
        # parser for one sentinel item beyond the UI limit so truncation is
        # detectable while parsing itself remains bounded.
        if normalized_path is not None:
            recent: deque[dict[str, Any]] = deque(maxlen=_MAX_TRAJECTORY_STEPS + 1)
            for line in raw.splitlines():
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(record, dict):
                    recent.append(record)
            steps = list(recent)
        else:
            steps = parse_agent_trajectory(raw, max_steps=_MAX_TRAJECTORY_STEPS + 1)
        steps_truncated = len(steps) > _MAX_TRAJECTORY_STEPS
        steps = _deduplicate_final_text(steps)
        if steps_truncated:
            steps = steps[-_MAX_TRAJECTORY_STEPS:]
        result = {
            "round_index": round_index,
            "role": role,
            "steps": steps,
            "step_count": len(steps),
            "raw_chars": len(raw),
            "trajectory_source": "normalized" if normalized_path is not None else "provider_raw",
        }
        if steps_truncated:
            result.update(
                {
                    "steps_truncated": True,
                    "warning": (
                        f"trajectory has more than {_MAX_TRAJECTORY_STEPS} steps; "
                        f"showing the latest {_MAX_TRAJECTORY_STEPS}"
                    ),
                }
            )
        return result

    def snapshot(self) -> dict[str, Any]:
        report = self.read_report()
        # The final reply is written before the aggregate report in the manager
        # completion path. Expose the role-scoped file immediately so a live
        # dashboard can render it without waiting for report.json.
        final_response, _ = _read_text_bounded(
            self._role_dir / "final_response.txt",
            _MAX_ROUND_TEXT_BYTES,
            tail=False,
            strict_parent=True,
        )
        if not final_response and isinstance(report.get("final_response"), str):
            final_response = report["final_response"]
        rounds = self.read_rounds()
        for item in rounds:
            index = item.get("round_index")
            if isinstance(index, int):
                item["roles"] = self.list_round_roles(index)
                # Trajectory byte sizes let the UI detect live growth (the files
                # are tee'd while a role runs) and refetch without a full reload.
                item["role_sizes"] = self._round_trajectory_sizes(index)
        return {
            "task": self.task or report.get("task", ""),
            "log_dir": str(self.log_dir),
            "runs": self.list_runs(),
            "current_run": self.current_run_id,
            "report": report,
            "final_response": final_response or "",
            "rounds": rounds,
            "round_count": len(rounds),
            "events": self.read_events(limit=200),
            "approvals": self.list_approvals(),
            "operator_messages": self.list_operator_messages(),
            "pending_injections": self.list_injections(),
            "control_enabled": self.control_enabled,
            "server_time": time.time(),
        }

    # ------------------------------------------------------------------
    # Human approval records (in-memory, shared across threads)
    # ------------------------------------------------------------------
    def create_approval(
        self,
        *,
        title: str,
        message: str = "",
        options: list[ApprovalOption] | None = None,
        answers: list[str] | None = None,
        allow_input: bool = True,
        input_label: str = "",
        context: dict[str, Any] | None = None,
    ) -> Approval:
        approval = Approval(
            approval_id=uuid.uuid4().hex[:12],
            title=title,
            message=message,
            options=list(options or []),
            answers=list(answers or []),
            allow_input=allow_input,
            input_label=input_label,
            context=context or {},
        )
        with self._lock:
            self._approvals[approval.approval_id] = approval
            self._approval_order.append(approval.approval_id)
        # Persist to the run's log dir so every human interaction is saved and
        # visible on the web even after the run ends or from a browsing session.
        self._persist_approval(approval.to_dict())
        return approval

    def resolve_approval(
        self,
        approval_id: str,
        *,
        action: str,
        reason: str = "",
        user_input: str = "",
        command_id: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        if not self.control_enabled:
            return False
        self._refresh_approval_from_disk(approval_id)
        normalized_action = str(action or "").strip()
        normalized_reason = reason.strip()
        normalized_input = user_input.strip()
        # The worker only understands the option values it issued.  Accept a
        # small backwards-compatible default vocabulary when an old approval
        # record omitted options, but never silently turn an arbitrary action
        # into "continue".
        with self._lock:
            approval = self._approvals.get(approval_id)
            if approval is None or approval.status == "resolved":
                return False
            allowed = {str(option.value).strip() for option in approval.options if str(option.value).strip()}
            if not allowed:
                allowed = {"continue", "stop"}
            if normalized_action not in allowed:
                return False
            if not approval.allow_input and normalized_input:
                return False
        payload = {
            "approval_id": approval_id,
            "action": normalized_action,
            "reason": normalized_reason,
            "user_input": normalized_input,
        }
        # The approval id is the durable uniqueness key.  Browser retries may
        # carry different Idempotency-Key values (or none at all), but one
        # checkpoint must never enqueue two decisions.  ControlBus serialises
        # the append across processes, closing the pending-check/append TOCTOU
        # window that the old in-memory check left open.
        canonical_command_id = f"approval:{approval_id}:resolve"
        existing = next(
            (
                item
                for item in reversed(self.control_bus.commands())
                if item.get("kind") == "resolve_approval"
                and str((item.get("payload") or {}).get("approval_id")) == approval_id
            ),
            None,
        )
        if existing is not None:
            existing_payload = existing.get("payload") if isinstance(existing.get("payload"), dict) else {}
            # An exact retry is idempotently accepted; a conflicting decision
            # is rejected and remains visible to the caller as HTTP 409.
            existing_key = str(existing_payload.get("idempotency_key") or "")
            requested_key = str(command_id or "")
            if requested_key and existing_key and requested_key != existing_key:
                return False
            return all(str(existing_payload.get(key, "")) == str(value) for key, value in payload.items())
        command = self.control_bus.append(
            "resolve_approval",
            {**payload, "idempotency_key": str(command_id or "")},
            created_by="operator",
            expected_revision=expected_revision,
            command_id=canonical_command_id,
        )
        persisted_payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
        persisted_key = str(persisted_payload.get("idempotency_key") or "")
        requested_key = str(command_id or "")
        if requested_key and persisted_key and requested_key != persisted_key:
            return False
        return all(str(persisted_payload.get(key, "")) == str(value) for key, value in payload.items())

    def _persist_approval(self, record: dict[str, Any]) -> None:
        """Append an approval record (pending or resolved) to the run log dir."""
        path = self._role_dir / "approvals.jsonl"
        fd: int | None = None
        parent_fd: int | None = None
        try:
            line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            # Keep the parent descriptor returned by the anchored walk; a
            # path-based mkdir followed by open would permit a swapped
            # ``role_management`` directory to redirect this append.
            parent_fd = _ensure_dir_fd_nofollow(path.parent)
            fd = _open_private_regular_at(parent_fd, path.name, os.O_WRONLY | os.O_APPEND)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fd = None
                try:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                except ImportError as exc:
                    raise OSError("secure approval-log locking is unavailable") from exc
                try:
                    fh.write(line)
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            # Approval persistence is diagnostic/control state. A read-only or
            # unavailable log must not crash the manager's execution loop.
            pass
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if parent_fd is not None:
                try:
                    os.close(parent_fd)
                except OSError:
                    pass

    def get_approval(self, approval_id: str) -> Approval | None:
        self._refresh_approval_from_disk(approval_id)
        # Only the worker's blocking wait consumes operator commands.  Read
        # APIs must remain observational; otherwise two dashboard processes can
        # race while a GET silently resolves an approval.
        self._apply_pending_resolutions()
        with self._lock:
            return self._approvals.get(approval_id)

    def _approval_from_record(self, record: dict[str, Any]) -> Approval | None:
        approval_id = record.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            return None
        options = [
            ApprovalOption(str(item.get("value", "")), str(item.get("label", "")), str(item.get("style", "")))
            for item in record.get("options", [])
            if isinstance(item, dict)
        ]
        return Approval(
            approval_id=approval_id,
            title=str(record.get("title", "")),
            message=str(record.get("message", "")),
            options=options,
            answers=[str(item) for item in record.get("answers", [])],
            allow_input=bool(record.get("allow_input", True)),
            input_label=str(record.get("input_label", "")),
            context=dict(record.get("context", {})) if isinstance(record.get("context"), dict) else {},
            status=str(record.get("status", "pending")),
            action=str(record.get("action", "")),
            reason=str(record.get("reason", "")),
            user_input=str(record.get("user_input", "")),
            created_at=float(record.get("created_at", time.time())),
            resolved_at=float(record["resolved_at"]) if record.get("resolved_at") else None,
        )

    def _refresh_approval_from_disk(self, approval_id: str) -> None:
        latest = next(
            (
                item
                for item in reversed(_read_jsonl(self._role_dir / "approvals.jsonl", strict_parent=True))
                if item.get("approval_id") == approval_id
            ),
            None,
        )
        if latest is None:
            return
        approval = self._approval_from_record(latest)
        if approval is None:
            return
        with self._lock:
            self._approvals[approval_id] = approval
            if approval_id not in self._approval_order:
                self._approval_order.append(approval_id)

    def _apply_pending_resolutions(self) -> None:
        for command in self.control_bus.pending():
            if command.get("kind") != "resolve_approval":
                continue
            payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
            approval_id = str(payload.get("approval_id", ""))
            self._refresh_approval_from_disk(approval_id)
            with self._lock:
                approval = self._approvals.get(approval_id)
                if approval is None or approval.status == "resolved":
                    continue
                action = str(payload.get("action", "")).strip()
                allowed = {str(option.value).strip() for option in approval.options if str(option.value).strip()}
                if not allowed:
                    allowed = {"continue", "stop"}
                user_input = str(payload.get("user_input", "")).strip()
                if action not in allowed or (not approval.allow_input and user_input):
                    self.control_bus.receipt(command, "rejected", message="invalid approval response")
                    continue
                approval.action = action
                approval.reason = str(payload.get("reason", "")).strip()
                approval.user_input = user_input
                approval.status = "resolved"
                approval.resolved_at = time.time()
                snapshot = approval.to_dict()
            self._persist_approval(snapshot)
            self.control_bus.receipt(command, "applied", result={"approval_id": approval_id})

    def update_approval_context(self, approval_id: str, **updates: Any) -> bool:
        """Update live setup/progress metadata and append a durable snapshot."""

        with self._lock:
            approval = self._approvals.get(approval_id)
            if approval is None:
                return False
            approval.context.update(updates)
            snapshot = approval.to_dict()
        self._persist_approval(snapshot)
        return True

    def list_approvals(self) -> list[dict[str, Any]]:
        # Merge on-disk records (so past/other-process interactions still show)
        # with in-memory records only when the latter is newer.  Reading a
        # snapshot must not consume control commands or let a stale API cache
        # overwrite a resolved durable record.
        merged: dict[str, dict[str, Any]] = {}
        for record in _read_jsonl(self._role_dir / "approvals.jsonl", strict_parent=True):
            approval_id = record.get("approval_id")
            if isinstance(approval_id, str):
                merged[approval_id] = record
        with self._lock:
            for approval_id in self._approval_order:
                candidate = self._approvals[approval_id].to_dict()
                existing = merged.get(approval_id)
                if existing is None or _approval_record_newer(candidate, existing):
                    merged[approval_id] = candidate
        items = list(merged.values())
        items.sort(key=lambda item: item.get("created_at") or 0.0)
        return items

    def has_pending_approval(self) -> bool:
        return any(item.get("status") == "pending" for item in self.list_approvals())

    # ------------------------------------------------------------------
    # Free-form operator instruction injections
    # ------------------------------------------------------------------
    def add_injection(
        self,
        text: str,
        *,
        command_id: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        if not self.control_enabled:
            return False
        text = (text or "").strip()
        if not text:
            return False
        self.control_bus.append(
            "inject_instruction",
            {"instructions": text},
            created_by="operator",
            expected_revision=expected_revision,
            command_id=command_id,
        )
        return True

    def list_injections(self) -> list[str]:
        return [
            str((item.get("payload") or {}).get("instructions", ""))
            for item in self.control_bus.pending()
            if item.get("kind") == "inject_instruction"
        ]

    def list_operator_messages(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return durable user-authored instruction messages for the timeline.

        Pending injections alone are insufficient: once the worker applies an
        instruction it disappears from ``pending()``, even though the message
        must remain visible after refresh. Commands are the append-only source
        of truth and receipts only enrich their delivery state.
        """
        bounded_limit = max(1, min(int(limit), 500))
        receipts = {
            str(item.get("command_id") or ""): item
            for item in self.control_bus.receipts()
            if item.get("command_id")
        }
        messages: list[dict[str, Any]] = []
        for command in self.control_bus.commands():
            if command.get("kind") != "inject_instruction":
                continue
            payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
            text = str(payload.get("instructions") or "").strip()
            command_id = str(command.get("command_id") or "").strip()
            if not text or not command_id:
                continue
            receipt = receipts.get(command_id) or {}
            try:
                created_at = float(command.get("created_at") or 0.0)
            except (TypeError, ValueError):
                created_at = 0.0
            messages.append(
                {
                    "id": command_id,
                    "text": text[:50_000],
                    "created_at": created_at,
                    "status": str(receipt.get("status") or "queued"),
                }
            )
        return messages[-bounded_limit:]

    def drain_injections(self) -> list[str]:
        drained: list[str] = []
        for command in self.control_bus.pending():
            if command.get("kind") != "inject_instruction":
                continue
            payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
            text = str(payload.get("instructions", "")).strip()
            if text:
                drained.append(text)
            self.control_bus.receipt(command, "applied", result={"instructions": text})
        return drained


def _open_nofollow(path: Path, *, directory: bool = False, strict_parent: bool = False) -> int:
    """Open a path for reading without following any path component symlink.

    ``Path.resolve()`` followed by ``Path.read_*`` is not sufficient for a
    worker-writable run directory: an attacker can replace the checked file
    (or one of its parent directories) between those operations.  On POSIX we
    walk from the filesystem root with ``openat`` and ``O_NOFOLLOW`` for every
    component.  A conservative final-component fallback keeps the helper
    usable on platforms without ``dir_fd`` support.
    """

    absolute = Path(os.path.abspath(os.fspath(path)))
    # macOS exposes /var (and a few other system roots) as symlinks.  Resolve
    # the parent once so the component walk starts from a canonical directory;
    # the final component is intentionally left unresolved and protected by
    # O_NOFOLLOW.
    path = absolute if strict_parent else Path(os.path.realpath(absolute.parent)) / absolute.name
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    flags = os.O_RDONLY | nofollow | cloexec | (directory_flag if directory else 0)
    if not directory:
        # Reject a worker-created FIFO without blocking the dashboard thread
        # while opening it.  Regular files ignore O_NONBLOCK.
        flags |= getattr(os, "O_NONBLOCK", 0)
    parts = path.parts
    if not parts or parts[0] != os.sep or len(parts) == 1 or os.open not in getattr(os, "supports_dir_fd", set()):
        return os.open(path, flags)

    root_fd = os.open(os.sep, os.O_RDONLY | directory_flag | nofollow | cloexec)
    current_fd = root_fd
    try:
        components = parts[1:]
        for component in components[:-1]:
            if component in {"", ".", ".."}:
                raise OSError("unsafe path component")
            next_fd = os.open(
                component,
                os.O_RDONLY | directory_flag | nofollow | cloexec,
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        final_component = components[-1]
        if final_component in {"", ".", ".."}:
            raise OSError("unsafe path component")
        try:
            result_fd = os.open(final_component, flags, dir_fd=current_fd)
        finally:
            os.close(current_fd)
        return result_fd
    except BaseException:
        try:
            os.close(current_fd)
        except OSError:
            pass
        raise


def _read_file_bounded(
    path: Path,
    max_bytes: int,
    *,
    tail: bool,
    strict_parent: bool = False,
) -> tuple[bytes | None, bool]:
    """Read a regular file through a no-follow descriptor with a byte cap.

    Returns ``(data, too_large)``.  For JSONL callers, ``tail=True`` retains a
    complete suffix when the file exceeds the cap.  Non-tail callers receive a
    bounded prefix together with ``too_large=True``; authoritative JSON and
    artifact callers reject that result, while human-readable round text can
    show the prefix with an explicit truncation marker.
    """

    try:
        fd = _open_nofollow(path, strict_parent=strict_parent)
    except OSError:
        return None, False
    try:
        metadata = os.fstat(fd)
        if not stat_module.S_ISREG(metadata.st_mode):
            return None, False
        size = int(metadata.st_size)
        start = max(0, size - max_bytes) if tail else 0
        if start:
            os.lseek(fd, start, os.SEEK_SET)
        # Read one extra byte so a file that grows after fstat cannot evade
        # the bound.
        data = bytearray()
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            data.extend(chunk)
            remaining -= len(chunk)
        too_large = len(data) > max_bytes
        raw = bytes(data[:max_bytes])
        if tail and start:
            first_newline = raw.find(b"\n")
            if first_newline < 0:
                return b"", True
            raw = raw[first_newline + 1 :]
        if tail and raw and not raw.endswith((b"\n", b"\r")):
            last_newline = raw.rfind(b"\n")
            raw = raw[: last_newline + 1] if last_newline >= 0 else b""
        return raw, too_large
    except OSError:
        return None, False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _read_text_bounded(
    path: Path,
    max_bytes: int,
    *,
    tail: bool = False,
    strict_parent: bool = False,
) -> tuple[str | None, bool]:
    data, too_large = _read_file_bounded(path, max_bytes, tail=tail, strict_parent=strict_parent)
    if data is None:
        return None, too_large
    return data.decode("utf-8", errors="replace"), too_large


def _infer_next_step(plan_text: str) -> str:
    """Route of an in-progress round, read from its ``manager_plan.txt``.

    Reuses the harness route parser so the UI can never drift from the routes the
    manager loop actually recognizes. An unparseable plan shows no route badge
    rather than the loop's internal ``invalid`` marker.
    """
    route = parse_role_manager_next_step(plan_text)
    return "" if route == "invalid" else route


def _deduplicate_final_text(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not steps or steps[-1].get("kind") != "result":
        return steps
    final_text = str(steps[-1].get("text") or "").strip()
    if not final_text:
        return steps
    for index in range(len(steps) - 2, -1, -1):
        if steps[index].get("kind") != "text":
            continue
        if str(steps[index].get("text") or "").strip() == final_text:
            return steps[:index] + steps[index + 1 :]
        break
    return steps


def _active_role_for_round(events: list[dict[str, Any]], round_index: int) -> tuple[str | None, bool]:
    starts = {
        "manager_round_start": "manager",
        "executor_role_start": "executor",
        "auditor_role_start": "auditor",
        "auditor_format_repair_start": "auditor_format_repair",
        "final_response_start": "final_response",
    }
    stops = {
        "manager_round_done",
        "executor_role_done",
        "auditor_role_done",
        "auditor_format_repair_done",
        "final_response_done",
        "managed_round_recorded",
    }
    active: str | None = None
    seen = False
    for event in events:
        name = event.get("event")
        if name in {"role_harness_cancelled", "role_harness_done"}:
            active = None
            seen = True
        elif event.get("round") == round_index:
            if name in starts:
                active = starts[name]
                seen = True
            elif name in stops:
                active = None
                seen = True
    return active, seen


def _read_json(path: Path, *, strict_parent: bool = False) -> Any:
    text, too_large = _read_text_bounded(path, _MAX_JSON_BYTES, tail=False, strict_parent=strict_parent)
    if text is None or too_large:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _read_jsonl(path: Path, *, strict_parent: bool = False) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    raw, _ = _read_text_bounded(path, _MAX_JSONL_BYTES, tail=True, strict_parent=strict_parent)
    if raw is None:
        return records
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line.encode("utf-8", errors="replace")) > _MAX_JSONL_LINE_BYTES:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
            if len(records) >= _MAX_JSONL_RECORDS:
                break
    return records


def _approval_record_newer(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    """Compare append-only approval snapshots without trusting process order."""

    candidate_time = float(candidate.get("resolved_at") or candidate.get("created_at") or 0.0)
    existing_time = float(existing.get("resolved_at") or existing.get("created_at") or 0.0)
    if candidate_time != existing_time:
        return candidate_time > existing_time
    # At equal timestamps a resolved record is strictly more informative than
    # a pending one.  This also handles clocks with coarse resolution.
    return str(candidate.get("status")) == "resolved" and str(existing.get("status")) != "resolved"
