"""Project existing DashboardState data into the stable Web snapshot."""

from __future__ import annotations

from typing import Any

from ..dashboard.state import DashboardState
from ..supervisor.lifecycle import canonical_lifecycle_status
from .events import EventTailer


_PROVENANCE_FIELDS = ("agent", "model", "role_configs", "workspace", "max_rounds", "prompt_language")
_MAX_FINAL_RESPONSE_CHARS = 512 * 1024


def _final_response(
    raw: dict[str, Any],
    report: dict[str, Any],
    rounds: list[Any],
) -> str:
    """Select the durable user-facing reply without falling back to audit text."""

    candidates: list[Any] = [raw.get("final_response"), report.get("final_response")]
    candidates.extend(
        item.get("final_response")
        for item in reversed(rounds)
        if isinstance(item, dict)
    )
    for value in candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        return value[:_MAX_FINAL_RESPONSE_CHARS]
    return ""


def _provenance(*sources: dict[str, Any] | None) -> dict[str, Any]:
    """Select bounded, durable run provenance from newest-authority sources.

    Supervisor ``owner.json`` is the primary source for managed runs; report
    and start-event data are useful fallbacks for historical/legacy runs.  The
    projection deliberately omits malformed or overlong values so an
    agent-written metadata file cannot inflate a run summary response.
    """

    result: dict[str, Any] = {}
    limits = {"agent": 64, "model": 256, "workspace": 4096}
    for field in _PROVENANCE_FIELDS:
        for source in sources:
            if not isinstance(source, dict) or field not in source:
                continue
            value = source.get(field)
            if field == "role_configs":
                cleaned = _safe_role_configs(value)
                if cleaned:
                    result[field] = cleaned
                    break
                continue
            if field == "max_rounds":
                if isinstance(value, bool):
                    continue
                if isinstance(value, int) and 1 <= value <= 1_000_000:
                    result[field] = value
                    break
                # JSON written by older integrations occasionally encoded an
                # integer as a decimal string. Accept only canonical digits.
                if isinstance(value, str) and value.isdecimal():
                    parsed = int(value)
                    if 1 <= parsed <= 1_000_000:
                        result[field] = parsed
                        break
                continue
            if field == "prompt_language":
                if value in {"en", "zh"}:
                    result[field] = value
                    break
                continue
            if value is None and field == "model":
                # ``None`` is meaningful: the run followed the agent/provider
                # default rather than selecting a custom model.
                result[field] = None
                break
            if not isinstance(value, str):
                continue
            text = value.strip()
            if not text or len(text) > limits[field] or "\x00" in text:
                continue
            result[field] = text
            break
    return result


def _safe_role_configs(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for role in ("manager", "executor", "auditor"):
        raw = value.get(role)
        if not isinstance(raw, dict):
            continue
        agent = raw.get("agent")
        model = raw.get("model")
        if agent not in {"codex", "claude_code", "deepseek_harness", "opencode"}:
            continue
        if not isinstance(model, str) or not model.strip() or len(model.strip()) > 256 or "\x00" in model:
            continue
        result[role] = {"agent": agent, "model": model.strip()}
        effort = raw.get("effort")
        if isinstance(effort, str) and 0 < len(effort) <= 64 and "\x00" not in effort:
            result[role]["effort"] = effort.strip()
    return result if len(result) == 3 else {}


def _status(raw: dict[str, Any], events: list[dict[str, Any]], approvals: list[dict[str, Any]]) -> str:
    report_status = raw.get("report", {}).get("status") if isinstance(raw.get("report"), dict) else None
    if report_status:
        return canonical_lifecycle_status(report_status)
    if any(item.get("status") == "pending" for item in approvals):
        return "waiting_approval"
    names = {str(item.get("event")) for item in events}
    if "role_harness_cancelled" in names:
        return "cancelled"
    if "role_harness_done" in names:
        return "completed"
    if events:
        return "running"
    return "idle"


def build_run_summary(item: dict[str, Any], *, state: DashboardState | None = None) -> dict[str, Any]:
    run_id = str(item.get("id") or "")
    status = str(item.get("status") or "")
    task = str(item.get("task") or "")
    provenance_sources: list[dict[str, Any] | None] = [item]
    if state is not None and (not status or not task):
        raw = state.snapshot()
        if not status:
            status = _status(raw, state.read_events(limit=5000), state.list_approvals())
        if not task:
            report = raw.get("report") if isinstance(raw.get("report"), dict) else {}
            task = str(raw.get("task") or report.get("task") or "")
    if state is not None and getattr(state, "runs_root", None) is not None:
        try:
            owner = state.control_bus.read_owner()
        except (OSError, ValueError, RuntimeError):
            owner = {}
        if isinstance(owner, dict):
            provenance_sources.insert(0, owner)
        try:
            report = state.read_report()
        except (OSError, ValueError, RuntimeError):
            report = {}
        provenance_sources.append(report if isinstance(report, dict) else None)
    summary = {
        "id": run_id,
        "task": task,
        "status": status or "unknown",
        "updated_at": item.get("mtime", 0.0),
        "log_dir": item.get("log_dir", ""),
    }
    summary.update(_provenance(*provenance_sources))
    return summary


def build_snapshot(state: DashboardState, *, run_id: str | None = None) -> dict[str, Any]:
    raw = state.snapshot()
    effective_run_id = run_id or state.current_run_id or "local"
    # Read the event log through the same tailer used by REST/WS replay.  This
    # keeps event ids absolute when the dashboard snapshot is limited to the
    # last 200 records and isolates malformed records to a diagnostic warning.
    tailer = EventTailer(
        state.role_dir / "events.jsonl",
        run_id=effective_run_id,
    )
    envelopes = tailer.read(limit=200)
    events = [item.to_dict() for item in envelopes]
    legacy_events = raw.get("events") if isinstance(raw.get("events"), list) else []
    approvals = raw.get("approvals") if isinstance(raw.get("approvals"), list) else []
    operator_messages = raw.get("operator_messages") if isinstance(raw.get("operator_messages"), list) else []
    rounds = raw.get("rounds") if isinstance(raw.get("rounds"), list) else []
    status = canonical_lifecycle_status(_status(raw, legacy_events, approvals))
    report = raw.get("report") if isinstance(raw.get("report"), dict) else {}
    owner: dict[str, Any] = {}
    if getattr(state, "runs_root", None) is not None:
        try:
            candidate_owner = state.control_bus.read_owner()
            if isinstance(candidate_owner, dict):
                owner = candidate_owner
        except (OSError, ValueError, RuntimeError):
            owner = {}
    # A supervised worker owns the task before its first Manager report exists.
    # Project that durable value immediately so Web does not render an active
    # run as "no task selected" while the first round is still in progress.
    task = str(raw.get("task") or report.get("task") or owner.get("task") or "")
    final_response = _final_response(raw, report, rounds)
    start_payload: dict[str, Any] = {}
    for event in reversed(legacy_events):
        if not isinstance(event, dict) or event.get("event") != "role_harness_start":
            continue
        if isinstance(event.get("payload"), dict):
            start_payload = event["payload"]
        else:
            start_payload = event
        # Legacy event records store ``workspace_path`` rather than
        # ``workspace``. Normalize just this safe, bounded field.
        if "workspace" not in start_payload and "workspace_path" in start_payload:
            start_payload = {**start_payload, "workspace": start_payload.get("workspace_path")}
        break
    provenance = _provenance(owner, report, start_payload)
    # ``active_*`` means work that can still change, never merely the newest
    # durable round.  A final Manager-only round is often appended after the
    # Auditor has already satisfied the task; falling back to that round made
    # clients render stale roles as active/waiting forever.
    active_round = None
    if status not in {"completed", "failed", "cancelled", "blocked", "incomplete"}:
        active_round = next(
            (
                item.get("round_index")
                for item in reversed(rounds)
                if isinstance(item, dict) and item.get("in_progress")
            ),
            None,
        )
    active_role = None
    if active_round is not None:
        for item in reversed(rounds):
            if isinstance(item, dict) and item.get("round_index") == active_round and item.get("active_role"):
                active_role = item["active_role"]
                break
    completion_satisfied = report.get("completion_satisfied")
    if not isinstance(completion_satisfied, bool):
        completion_satisfied = None
    completion_authority = report.get("completion_authority")
    if completion_authority is not None:
        completion_authority = str(completion_authority)
    report_status = report.get("status")
    if report_status is not None:
        report_status = canonical_lifecycle_status(str(report_status))
    exit_code = report.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        exit_code = None
    failure_reason = report.get("failure_reason")
    if failure_reason is not None:
        failure_reason = str(failure_reason)
    return {
        "schema_version": 1,
        "run": {
            "id": effective_run_id,
            "status": canonical_lifecycle_status(status),
            "started_at": report.get("started_at"),
            "finished_at": report.get("finished_at"),
            "log_dir": raw.get("log_dir", ""),
            "completion_satisfied": completion_satisfied,
            "completion_authority": completion_authority,
            "report_status": report_status,
            "exit_code": exit_code,
            "failure_reason": failure_reason,
            "final_response": final_response,
            **provenance,
        },
        "mission": {
            "task": task,
            "contract_path": "task_contract.txt",
            "plan_path": "manager_plan.txt",
            "verified_state_path": "task_state.txt",
            "report_path": "report.json",
        },
        "rounds": rounds,
        "active_round": active_round,
        "active_role": active_role,
        "events": events,
        "approvals": approvals,
        "operator_messages": operator_messages,
        "controls": {
            "can_inject": bool(raw.get("control_enabled")),
            # Approval/instruction control can attach directly to a worker,
            # while lifecycle signals require a RunSupervisor-owned process.
            # Supervised snapshots overlay this value in the API server.
            "can_abort": False,
            "can_resume": False,
        },
        "diagnostics": {
            "last_event_id": events[-1]["event_id"] if events else None,
            "event_count": len(events),
            "warnings": tailer.last_warnings,
            "cursor_gap": False,
            "resync_required": False,
        },
        "legacy": raw,
    }
