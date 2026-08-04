"""Shared dashboard state: reads harness logs and holds human-approval records.

All dashboard logic lives inside this package. The rest of the harness only
imports :func:`lh_harness.dashboard.start_dashboard` and the optional round
hook, so no harness behavior changes when the dashboard is not enabled.

The state object is shared between two worlds:

* the async management loop (via the round hook / approval gate), and
* the HTTP server thread (serving the web UI + JSON API).

On-disk logs are always read fresh from ``log_dir`` so the UI reflects live
progress. Approval records are kept in memory and guarded by a lock.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..role_prompts import parse_role_manager_next_step


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
    ) -> None:
        # When only a runs_root is given (manual dashboard browsing), the newest
        # run is auto-selected so the UI shows something immediately; the user
        # can switch to any other run from the UI.
        self.runs_root = Path(runs_root) if runs_root else None
        resolved = Path(log_dir) if log_dir else None
        if resolved is None and self.runs_root is not None:
            runs = self._scan_runs()
            if runs:
                resolved = Path(runs[0]["log_dir"])
        self.log_dir = resolved or Path(".")
        self.task = task
        self._lock = threading.Lock()
        self._approvals: dict[str, Approval] = {}
        self._approval_order: list[str] = []
        # Free-form operator notes to inject into upcoming manager prompts,
        # independent of a blocking approval checkpoint.
        self._pending_injections: list[str] = []

    # ------------------------------------------------------------------
    # Run selection (manual dashboard browsing across runs/<id>/logs)
    # ------------------------------------------------------------------
    def _scan_runs(self) -> list[dict[str, Any]]:
        root = self.runs_root
        if root is None or not root.is_dir():
            return []
        runs: list[dict[str, Any]] = []
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            # A run is any subdir containing logs/role_management.
            log_dir = entry / "logs"
            role_dir = log_dir / "role_management"
            if not role_dir.is_dir():
                continue
            try:
                mtime = role_dir.stat().st_mtime
            except OSError:
                mtime = 0.0
            report = _read_json(log_dir / "report.json")
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
        if root is None or not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
            return False
        log_dir = (root / run_id / "logs").resolve()
        try:
            log_dir.relative_to(root.resolve())
        except ValueError:
            return False
        if not (log_dir / "role_management").is_dir():
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
        return self.log_dir / "role_management"

    def read_report(self) -> dict[str, Any]:
        for candidate in (self.log_dir / "report.json", self._role_dir / "report.json"):
            data = _read_json(candidate)
            if isinstance(data, dict):
                return data
        return {}

    def read_rounds(self) -> list[dict[str, Any]]:
        # rounds.jsonl / report.json are only written when a round *finishes*.
        # To reflect live progress we also scan the per-round directories, whose
        # artifact files (manager_plan.txt, executor_output.txt, ...) are written
        # incrementally while a round is still running.
        recorded: dict[int, dict[str, Any]] = {}
        for item in _read_jsonl(self._role_dir / "rounds.jsonl"):
            index = item.get("round_index")
            if isinstance(index, int):
                recorded[index] = item  # append-only ledger: keep the latest
        if not recorded:
            report = self.read_report()
            report_rounds = report.get("rounds")
            if isinstance(report_rounds, list):
                for item in report_rounds:
                    index = item.get("round_index") if isinstance(item, dict) else None
                    if isinstance(index, int):
                        recorded[index] = item

        merged = dict(recorded)
        for index, live in self._scan_round_dirs().items():
            # Recorded (finished) rounds are authoritative; only add rounds that
            # exist on disk but have not been flushed to the ledger yet.
            if index not in merged:
                merged[index] = live
        return [merged[key] for key in sorted(merged)]

    def _scan_round_dirs(self) -> dict[int, dict[str, Any]]:
        rounds_root = self._role_dir / "rounds"
        result: dict[int, dict[str, Any]] = {}
        if not rounds_root.is_dir():
            return result
        for entry in rounds_root.iterdir():
            if not entry.is_dir():
                continue
            match = re.match(r"round_(\d+)$", entry.name)
            if not match:
                continue
            result[int(match.group(1))] = self._round_from_dir(int(match.group(1)), entry)
        return result

    def _round_from_dir(self, index: int, round_dir: Path) -> dict[str, Any]:
        def _text(name: str) -> str:
            try:
                return (round_dir / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""

        plan_text = _text("manager_plan.txt")
        return {
            "round_index": index,
            "next_step": _infer_next_step(plan_text),
            "plan_text": plan_text,
            "task_state": _text("task_state.txt"),
            "executor_output": _text("executor_output.txt"),
            "auditor_report": _text("auditor_report.txt"),
            "harness_feedback": _text("harness_feedback.txt"),
            "related_report_refs": [],
            "executor_status": {},
            "auditor_status": {},
            "in_progress": True,
        }

    def read_events(self, *, limit: int = 500) -> list[dict[str, Any]]:
        events = _read_jsonl(self._role_dir / "events.jsonl")
        return events[-limit:]

    def list_round_artifacts(self, round_index: int) -> list[str]:
        round_dir = self._role_dir / "rounds" / f"round_{round_index:03d}"
        if not round_dir.is_dir():
            return []
        return sorted(p.name for p in round_dir.iterdir() if p.is_file())

    def read_round_artifact(self, round_index: int, name: str) -> str | None:
        # Guard against path traversal: only allow plain file names.
        if "/" in name or "\\" in name or name in {"", ".", ".."}:
            return None
        round_dir = self._role_dir / "rounds" / f"round_{round_index:03d}"
        target = (round_dir / name).resolve()
        try:
            target.relative_to(round_dir.resolve())
        except ValueError:
            return None
        if not target.is_file():
            return None
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    # ------------------------------------------------------------------
    # Full role trajectories (parsed Claude stream-json for visualization)
    # ------------------------------------------------------------------
    def list_round_roles(self, round_index: int) -> list[str]:
        """Roles that have a saved raw trajectory for the given round."""
        round_dir = self._role_dir / "rounds" / f"round_{round_index:03d}"
        if not round_dir.is_dir():
            return []
        roles: list[str] = []
        for role in ("manager", "executor", "auditor", "auditor_format_repair"):
            if self._trajectory_path(round_dir, role) is not None:
                roles.append(role)
        return roles

    @staticmethod
    def _trajectory_path(round_dir: Path, role: str) -> Path | None:
        # New runs save .jsonl; keep reading .txt for older runs.
        for suffix in (".jsonl", ".txt"):
            candidate = round_dir / f"{role}_raw_trajectory{suffix}"
            if candidate.is_file():
                return candidate
        return None

    def _round_trajectory_sizes(self, round_index: int) -> dict[str, int]:
        """Current byte size of each role's trajectory file (for live refresh)."""
        round_dir = self._role_dir / "rounds" / f"round_{round_index:03d}"
        sizes: dict[str, int] = {}
        if not round_dir.is_dir():
            return sizes
        for role in ("manager", "executor", "auditor", "auditor_format_repair"):
            path = self._trajectory_path(round_dir, role)
            if path is not None:
                try:
                    sizes[role] = path.stat().st_size
                except OSError:
                    sizes[role] = 0
        return sizes

    def read_trajectory(self, round_index: int, role: str) -> dict[str, Any] | None:
        """Parse a role's raw Claude stream-json trajectory into ordered steps."""
        if role not in {"manager", "executor", "auditor", "auditor_format_repair"}:
            return None
        round_dir = self._role_dir / "rounds" / f"round_{round_index:03d}"
        traj_path = self._trajectory_path(round_dir, role)
        if traj_path is None:
            return None
        try:
            raw = traj_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        steps = _parse_claude_trajectory(raw)
        return {
            "round_index": round_index,
            "role": role,
            "steps": steps,
            "step_count": len(steps),
            "raw_chars": len(raw),
        }

    def snapshot(self) -> dict[str, Any]:
        report = self.read_report()
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
            "rounds": rounds,
            "round_count": len(rounds),
            "events": self.read_events(limit=200),
            "approvals": self.list_approvals(),
            "pending_injections": self.list_injections(),
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
    ) -> bool:
        with self._lock:
            approval = self._approvals.get(approval_id)
            if approval is None or approval.status == "resolved":
                return False
            # Any option value is accepted, so new option sets need no changes.
            approval.action = str(action)
            approval.reason = reason.strip()
            approval.user_input = user_input.strip()
            approval.status = "resolved"
            approval.resolved_at = time.time()
            snapshot = approval.to_dict()
        self._persist_approval(snapshot)  # record the resolution (choice/answer)
        return True

    def _persist_approval(self, record: dict[str, Any]) -> None:
        """Append an approval record (pending or resolved) to the run log dir."""
        path = self._role_dir / "approvals.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            pass

    def get_approval(self, approval_id: str) -> Approval | None:
        with self._lock:
            return self._approvals.get(approval_id)

    def list_approvals(self) -> list[dict[str, Any]]:
        # Merge on-disk records (so past/other-process interactions still show)
        # with in-memory approvals (authoritative for the live run). The JSONL is
        # append-only, so the latest line per approval_id wins.
        merged: dict[str, dict[str, Any]] = {}
        for record in _read_jsonl(self._role_dir / "approvals.jsonl"):
            approval_id = record.get("approval_id")
            if isinstance(approval_id, str):
                merged[approval_id] = record
        with self._lock:
            for approval_id in self._approval_order:
                merged[approval_id] = self._approvals[approval_id].to_dict()
        items = list(merged.values())
        items.sort(key=lambda item: item.get("created_at") or 0.0)
        return items

    def has_pending_approval(self) -> bool:
        with self._lock:
            return any(item.status == "pending" for item in self._approvals.values())

    # ------------------------------------------------------------------
    # Free-form operator instruction injections
    # ------------------------------------------------------------------
    def add_injection(self, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        with self._lock:
            self._pending_injections.append(text)
        return True

    def list_injections(self) -> list[str]:
        with self._lock:
            return list(self._pending_injections)

    def drain_injections(self) -> list[str]:
        with self._lock:
            drained = list(self._pending_injections)
            self._pending_injections.clear()
        return drained


def _parse_claude_trajectory(raw: str) -> list[dict[str, Any]]:
    """Turn a Claude Code stream-json log into ordered, UI-friendly steps.

    Emits one step per meaningful event: session init, assistant thinking, text,
    tool calls, tool results, and the final result. Non-JSON or noise lines
    (e.g. thinking_tokens deltas) are ignored.
    """
    steps: list[dict[str, Any]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        rtype = record.get("type")
        if rtype == "system":
            subtype = record.get("subtype")
            if subtype == "init":
                servers = record.get("mcp_servers") or []
                names = [s.get("name") for s in servers if isinstance(s, dict)]
                steps.append(
                    {
                        "kind": "session",
                        "model": record.get("model", ""),
                        "cwd": record.get("cwd", ""),
                        "mcp_servers": names,
                        "tool_count": len(record.get("tools") or []),
                    }
                )
            # skip thinking_tokens and other noisy system deltas
            continue
        if rtype == "assistant":
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            for block in message.get("content", []) or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "thinking":
                    text = block.get("thinking")
                    if isinstance(text, str) and text.strip():
                        steps.append({"kind": "thinking", "text": text})
                elif btype == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        steps.append({"kind": "text", "text": text})
                elif btype == "tool_use":
                    steps.append(
                        {
                            "kind": "tool_use",
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "input": block.get("input") or {},
                        }
                    )
            continue
        if rtype == "user":
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            content = message.get("content")
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                text, images = _tool_result_to_text(block.get("content"))
                steps.append(
                    {
                        "kind": "tool_result",
                        "tool_use_id": block.get("tool_use_id", ""),
                        "text": text,
                        "images": images,  # list of data URLs (base64 embedded)
                        "has_image": bool(images),
                        "is_error": bool(block.get("is_error")),
                    }
                )
            continue
        if rtype == "result":
            result_text = record.get("result", "") if isinstance(record.get("result"), str) else ""
            # Claude's final `result` is a copy of the last assistant text block,
            # so it would render twice. Drop the duplicated text step and keep
            # only the richer `result` step (which also carries run metadata).
            for i in range(len(steps) - 1, -1, -1):
                if steps[i]["kind"] == "text":
                    if steps[i].get("text", "").strip() == result_text.strip():
                        steps.pop(i)
                    break
            steps.append(
                {
                    "kind": "result",
                    "text": result_text,
                    "is_error": bool(record.get("is_error")),
                    "duration_ms": record.get("duration_ms"),
                    "num_turns": record.get("num_turns"),
                    "cost_usd": record.get("total_cost_usd"),
                }
            )
    return steps


def _tool_result_to_text(content: Any) -> tuple[str, list[str]]:
    """Flatten a tool_result payload into text plus embedded image data URLs.

    Claude tool results carry screenshots as base64 image blocks. We turn each
    into a ``data:`` URL so the dashboard can render them inline without any
    extra file writes.
    """
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return ("" if content is None else str(content)), []
    parts: list[str] = []
    images: list[str] = []
    for block in content:
        if isinstance(block, dict):
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif btype == "image":
                data_url = _image_block_to_data_url(block)
                if data_url:
                    images.append(data_url)
                    parts.append("[image]")
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts), images


def _image_block_to_data_url(block: dict[str, Any]) -> str:
    source = block.get("source")
    if not isinstance(source, dict):
        return ""
    data = source.get("data")
    if not isinstance(data, str) or not data:
        return ""
    media_type = source.get("media_type") or "image/png"
    return f"data:{media_type};base64,{data}"


def _infer_next_step(plan_text: str) -> str:
    """Route of an in-progress round, read from its ``manager_plan.txt``.

    Reuses the harness route parser so the UI can never drift from the routes the
    manager loop actually recognizes. An unparseable plan shows no route badge
    rather than the loop's internal ``invalid`` marker.
    """
    route = parse_role_manager_next_step(plan_text)
    return "" if route == "invalid" else route


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return records
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records
