"""Server-side state for the recursive-decomposition view (PLAN.md §11).

Bridges three worlds without coupling them:

* the pipeline driver (possibly running in this process, possibly in a
  detached ``waypoint run`` process — the state never tells the
  difference, because the only contract is the run directory),
* an HTTP server thread (snapshots for a web UI, SSE pushes) if one is
  ever mounted,
* the operator (approve/amend/reopen/halt actions).

Everything durable is read fresh from disk on every call — the same
"on-disk logs are always read fresh" rule the shipped dashboard state
already follows. The only in-process bookkeeping is the map of hosted
driver threads and the attached-run pointer.

The approval protocol is ``pipeline/approvals.py``'s: the driver creates
``pending`` records in ``approvals.jsonl`` and polls until a ``resolved``
record lands; this state resolves by appending that record (and, for
state-created approvals like amend/triage/reopen, dispatches the follow-up
job in a worker thread). Any other surface — a second browser, the CLI
``approve`` command — resolves the same file, so no surface owns a run.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..v0.events import EventLog
from ..v0.run_dir import events_path, manifest_path, node_artifact_path, node_scratch_dir, spec_path
from ..v1.gates import estimate_tokens
from ..v1.tree import TaskTree
from ..v2.contract import load_contract
from ..v2.run_dir import contract_path, spine_path
from ..v3.run_dir import (
    assembly_checks_path,
    assembly_index_path,
    assembly_output_path,
    compile_log_path,
    versions_dir,
)
from ..pipeline import approvals as approval_store
from ..pipeline.driver import (
    RunOptions,
    RecursiveDriver,
    amend_and_revalidate,
    apply_triage,
    reopen_node,
)
from ..pipeline.run_dir import halt_path, jobs_path, phase_path, run_spec_path

_DEFAULT_RUN_ID_PREFIX = "rec"


def _now() -> float:
    return time.time()


class RecursiveRunState:
    """Thread-safe per-server store for the recursive decomposition view."""

    def __init__(self, runs_root: str | Path | None = None, *, control_enabled: bool = True) -> None:
        self.runs_root = Path(runs_root) if runs_root else None
        self.control_enabled = control_enabled
        self._lock = threading.Lock()
        self._attached: str | None = None
        self._hosts: dict[str, threading.Thread] = {}

    # ------------------------------------------------------------------
    # Run scanning / attachment (read-only browsing across runs_root)
    # ------------------------------------------------------------------
    def list_runs(self) -> list[dict[str, Any]]:
        if self.runs_root is None or not self.runs_root.is_dir():
            return []
        runs: list[dict[str, Any]] = []
        for entry in sorted(self.runs_root.iterdir()):
            if not entry.is_dir():
                continue
            # A recursive run owns events.jsonl at its root.
            if not (entry / "events.jsonl").exists():
                continue
            phase = _read_json(phase_path(entry)) or {}
            spec = _read_json(run_spec_path(entry)) or {}
            runs.append(
                {
                    "id": entry.name,
                    "goal": str(spec.get("goal", "")),
                    "phase": str(phase.get("phase", "")),
                    "status": str(phase.get("status", "created")),
                    "detail": str(phase.get("detail", "")),
                    "mtime": _dir_mtime(entry),
                    "attached": entry.name == self._attached,
                }
            )
        runs.sort(key=lambda item: item["mtime"], reverse=True)
        return runs

    def attach(self, run_id: str) -> bool:
        if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
            return False
        if self.runs_root is None:
            return False
        run_dir = (self.runs_root / run_id).resolve()
        try:
            run_dir.relative_to(self.runs_root.resolve())
        except ValueError:
            return False
        if not (run_dir / "events.jsonl").is_file():
            return False
        with self._lock:
            self._attached = run_id
        return True

    @property
    def attached_run_id(self) -> str | None:
        with self._lock:
            return self._attached

    def _attached_dir(self) -> Path | None:
        if self.runs_root is None:
            return None
        run_id = self.attached_run_id
        if not run_id:
            return None
        return self.runs_root / run_id

    # ------------------------------------------------------------------
    # Snapshots (always read fresh; the disk is authoritative)
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        run_dir = self._attached_dir()
        if run_dir is None:
            return {"attached": False, "runs": self.list_runs(), "server_time": _now()}
        spec = _read_json(run_spec_path(run_dir)) or {}
        phase = _read_json(phase_path(run_dir)) or {}
        events = EventLog(events_path(run_dir)).read_all()
        tree = _load_tree(run_dir)
        approvals = approval_store.read_all(run_dir)
        return {
            "attached": True,
            "run_id": self.attached_run_id,
            "runs": self.list_runs(),
            "goal": str(spec.get("goal", "")),
            "backend": str(spec.get("backend", "")),
            "phase": str(phase.get("phase", "")),
            "phase_status": str(phase.get("status", "")),
            "phase_detail": str(phase.get("detail", "")),
            "phases": _phase_map(events),
            "tree": _tree_summary(tree),
            "tree_counts": _count_statuses(tree),
            "approvals": [item.to_dict() for item in approvals],
            "pending_approvals": [item.to_dict() for item in approvals if item.status == "pending"],
            "events": [e for e in events[-50:]],
            "events_count": len(events),
            "jobs": _read_jobs(run_dir),
            "halted": halt_path(run_dir).exists(),
            "has_spec": _has_content(spec_path(run_dir)),
            "has_contract": contract_path(run_dir).exists(),
            "has_assembly": assembly_output_path(run_dir).exists(),
            "server_time": _now(),
        }

    def events_tail(self, after: int = 0) -> list[dict[str, Any]]:
        run_dir = self._attached_dir()
        if run_dir is None:
            return []
        events = EventLog(events_path(run_dir)).read_all()
        return [e for e in events[after:]]

    # ------------------------------------------------------------------
    # Hosted runs (hosted state drives the driver in a background thread)
    # ------------------------------------------------------------------
    def start_run(self, body: dict[str, Any], *, driver=None) -> tuple[str | None, str]:
        """Create and start a recursive run. Returns (run_id, error)."""
        if not self.control_enabled:
            return None, "control is disabled"
        if self.runs_root is None:
            return None, "no runs_root configured"
        goal = str(body.get("goal", "")).strip()
        if not goal:
            return None, "goal is required"
        run_id = str(body.get("run_id") or "").strip()
        if not run_id:
            run_id = f"{_DEFAULT_RUN_ID_PREFIX}{int(_now())}{uuid.uuid4().hex[:6]}"
        if "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
            return None, "invalid run_id"
        run_dir = self.runs_root / run_id
        from ..v0.run_dir import create_run_dir

        create_run_dir(self.runs_root, run_id)
        options = RunOptions(
            goal=goal,
            backend=str(body.get("backend") or _default_backend()),
            model=body.get("model") or None,
            source_text=str(body.get("source", "")),
            compile_command=body.get("compile_command") or None,
            research_plan=_parse_plan_payload(body.get("research_plan")),
            max_rounds=int(body.get("max_rounds", 100)),
            max_attempts=int(body.get("max_attempts", 3)),
        )
        if driver is None:
            driver = self._default_driver(run_dir, options)
        thread = threading.Thread(
            target=_host_driver, args=(run_dir, driver), name=f"recursive-{run_id}", daemon=True
        )
        with self._lock:
            self._hosts[run_id] = thread
            self._attached = run_id
        thread.start()
        return run_id, ""

    def _default_driver(self, run_dir: Path, options: RunOptions) -> RecursiveDriver:
        from ..v1.provider import OpenAICompatibleProvider

        return RecursiveDriver(run_dir, provider=OpenAICompatibleProvider(model=options.model), options=options)

    def is_hosted(self, run_id: str | None = None) -> bool:
        with self._lock:
            return (run_id or self._attached) in self._hosts

    def halt(self, value: bool) -> bool:
        run_dir = self._attached_dir()
        if run_dir is None:
            return False
        flag = halt_path(run_dir)
        if value:
            flag.touch()
        else:
            try:
                flag.unlink()
            except OSError:
                pass
        return True

    # ------------------------------------------------------------------
    # Approval protocol (the disk file is the authority)
    # ------------------------------------------------------------------
    def resolve_approval(self, approval_id: str, *, action: str = "", user_input: str = "") -> bool:
        run_dir = self._attached_dir()
        if run_dir is None:
            return False
        if not self.control_enabled:
            return False
        record = _find_approval(run_dir, approval_id)
        if record is None or record.status == "resolved":
            return False
        resolved = record.resolve(action=action, user_input=user_input)
        approval_store.append(run_dir, resolved)
        if action == "apply":
            self._dispatch_resolved_job(run_dir, resolved)
        return True

    def _dispatch_resolved_job(self, run_dir: Path, approval: approval_store.Approval) -> None:
        kind = approval.kind
        if kind == "amend":
            self._spawn_job(run_dir, "amend", _run_amend_job, approval.approval_id, approval_id=approval.approval_id)
        elif kind == "triage":
            self._spawn_job(run_dir, "triage", _run_triage_job, approval.approval_id, approval_id=approval.approval_id)
        elif kind == "reopen":
            self._spawn_job(run_dir, "reopen", _run_reopen_job, approval.approval_id, approval_id=approval.approval_id)

    def _spawn_job(self, run_dir: Path, kind: str, target: Any, job_id: str, **kwargs: Any) -> None:
        _append_job(run_dir, {"job_id": job_id, "kind": kind, "status": "running", "ts": _now(), "detail": ""})
        thread = threading.Thread(
            target=_job_thread, args=(run_dir, kind, job_id, target), kwargs=kwargs, daemon=True
        )
        thread.start()

    # ------------------------------------------------------------------
    # Operator actions that *create* approvals (never run jobs directly)
    # ------------------------------------------------------------------
    def request_amend(self, text: str, reason: str = "web amendment") -> dict[str, Any] | None:
        """§10: show the re-validation cost estimate *before* running it.
        Creates an approval whose apply half runs amend + re-validation."""
        run_dir = self._attached_dir()
        if run_dir is None:
            return None
        text = (text or "").strip()
        if not text:
            return None
        contract_now = load_contract(run_dir) if contract_path(run_dir).exists() else ""
        tree = _load_tree(run_dir)
        from ..v3.revalidate import estimate_revalidation_cost

        estimate = estimate_revalidation_cost(run_dir, tree, contract_now or "amendment preview")
        message = (
            f"Append to the contract:\n\n{text}\n\n"
            f"Re-validation will re-review {estimate.node_count} completed node(s) "
            f"(~{estimate.estimated_tokens} tokens). Apply?"
        )
        approval = approval_store.Approval.create(
            "amend",
            title="Amend contract",
            message=message,
            options=[
                {"value": "apply", "label": "Apply amendment", "style": "primary"},
                {"value": "cancel", "label": "Cancel"},
            ],
            allow_input=False,
            context={"text": text, "reason": reason, "estimate": {"nodes": estimate.node_count, "tokens": estimate.estimated_tokens}},
        )
        approval_store.append(run_dir, approval)
        return approval.to_dict()

    def request_reopen(self, node_id: str, defect: str) -> dict[str, Any] | None:
        run_dir = self._attached_dir()
        if run_dir is None:
            return None
        defect = (defect or "").strip()
        if not defect:
            return None
        approval = approval_store.Approval.create(
            "reopen",
            title=f"Reopen {node_id} for a scoped repair",
            message=f"Defect: {defect}",
            options=[
                {"value": "apply", "label": "Dispatch repair", "style": "primary"},
                {"value": "cancel", "label": "Cancel"},
            ],
            allow_input=False,
            context={"node_id": node_id, "defect": defect},
        )
        approval_store.append(run_dir, approval)
        return approval.to_dict()

    # ------------------------------------------------------------------
    # Per-node view
    # ------------------------------------------------------------------
    def node_detail(self, node_id: str) -> dict[str, Any] | None:
        run_dir = self._attached_dir()
        if run_dir is None:
            return None
        tree = _load_tree(run_dir)
        node = tree.nodes.get(node_id)
        if node is None:
            return None
        artifact = _read_text(node_artifact_path(run_dir, node_id))
        from ..v1.gates import evaluate_gates

        gate_results = [
            {"gate": result.gate, "passed": result.passed, "detail": result.detail}
            for result in evaluate_gates(node.gates, artifact)
        ]
        audit = _read_json(run_dir / "audit" / f"{node_id}.json") or {}
        manifest_lines = [
            item for item in _read_jsonl(manifest_path(run_dir)) if item.get("node") == node_id
        ]
        versions = _list_versions(run_dir, node_id)
        promotion = _read_json(node_scratch_dir(run_dir, node_id) / "promotion.json") or {}
        inputs = [
            {
                "ref": item,
                "tokens": _input_tokens(run_dir, item),
                "exists": _input_exists(run_dir, item),
            }
            for item in node.inputs
        ]
        return {
            "id": node.id,
            "brief": node.brief,
            "status": node.status,
            "attempts": node.attempts,
            "shape": node.shape,
            "gates": node.gates,
            "gate_results": gate_results,
            "judgment": node.judgment,
            "rubric": node.rubric,
            "inputs": inputs,
            "budget": {"tokens": node.budget.tokens, "calls": node.budget.calls},
            "depends_on": node.depends_on,
            "artifact": artifact,
            "artifact_tokens": estimate_tokens(artifact),
            "audit": audit,
            "manifest": manifest_lines[-1] if manifest_lines else None,
            "versions": versions,
            "promotion": promotion.get("promotion", ""),
        }

    def artifact(self, node_id: str) -> str | None:
        run_dir = self._attached_dir()
        if run_dir is None:
            return None
        return _read_text(node_artifact_path(run_dir, node_id)) if _safe_node_id(node_id) else None

    def version_snapshot(self, node_id: str, tag: str) -> str | None:
        run_dir = self._attached_dir()
        if run_dir is None or not _safe_node_id(node_id) or not _safe_node_id(tag):
            return None
        target = (versions_dir(run_dir, node_id) / tag).resolve()
        try:
            target.relative_to(versions_dir(run_dir, node_id).resolve())
        except ValueError:
            return None
        return _read_text(target)

    def trace(self, node_id: str) -> str | None:
        run_dir = self._attached_dir()
        if run_dir is None or not _safe_node_id(node_id):
            return None
        return _read_text(run_dir / "scratch" / node_id / "trace.jsonl")

    def spec_text(self) -> str:
        run_dir = self._attached_dir()
        if run_dir is None:
            return ""
        return _read_text(spec_path(run_dir)) or ""

    def contract_text(self) -> str:
        run_dir = self._attached_dir()
        if run_dir is None:
            return ""
        return _read_text(contract_path(run_dir)) or ""

    def spine_text(self) -> str:
        run_dir = self._attached_dir()
        if run_dir is None:
            return ""
        raw = _read_text(spine_path(run_dir))
        if not raw:
            return ""
        try:
            units = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        return "\n".join(f"- {u.get('id')}: {u.get('label')} ({u.get('tokens')} tokens)" for u in units if isinstance(u, dict))

    def manifest_lines(self) -> list[dict[str, Any]]:
        run_dir = self._attached_dir()
        if run_dir is None:
            return []
        return _read_jsonl(manifest_path(run_dir))

    def assembly(self) -> dict[str, Any]:
        run_dir = self._attached_dir()
        if run_dir is None:
            return {}
        return {
            "output": _read_text(assembly_output_path(run_dir)) or "",
            "index": _read_text(assembly_index_path(run_dir)) or "",
            "checks": _read_json(assembly_checks_path(run_dir)) or {},
            "compile_log": _read_text(compile_log_path(run_dir)) or "",
        }


def _default_backend() -> str:
    import os

    return os.getenv("WAYPOINT_BACKEND", "gptme")


def _host_driver(run_dir: Path, driver: RecursiveDriver) -> None:
    try:
        import asyncio

        report = asyncio.run(driver.run())
        _set_phase(run_dir, report.phase, report.status, report.detail)
    except Exception as exc:  # noqa: BLE001 — surface into phase.json, not the thread
        _set_phase(run_dir, "error", "error", str(exc))


def _set_phase(run_dir: Path, phase: str, status: str, detail: str = "") -> None:
    phase_path(run_dir).write_text(
        json.dumps({"phase": phase, "status": status, "detail": detail, "ts": _now()}, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _run_amend_job(run_dir: Path, approval_id: str) -> None:
    from ..v1.provider import OpenAICompatibleProvider

    approval = _find_approval(run_dir, approval_id)
    if approval is None:
        _finish_job(run_dir, approval_id, "amend", "failed", "approval record missing")
        return
    context = approval.context or {}
    try:
        options, provider, env, factory = _runtime_for(run_dir)
        result = asyncio_run(amend_and_revalidate(run_dir, rule_text=context.get("text", ""), reason=context.get("reason", "web amendment"), provider=provider))
        counts = result["counts"]
        lines = ["Re-validation of the amended contract:", "", f"clean: {counts['clean']}   patchable: {counts['patchable']}   regenerate: {counts['regenerate']}", "", "Non-clean nodes:", ""]
        for node_id, record in result["triage"].items():
            lines.append(f"- {node_id} [{record['classification']}]")
        triage_approval = approval_store.Approval.create(
            "triage",
            title="Apply re-validation triage",
            message="\n".join(lines),
            options=[
                {"value": "apply", "label": "Dispatch repairs", "style": "primary"},
                {"value": "cancel", "label": "Leave stale"},
            ],
            allow_input=False,
            context={"triage": result["triage"], "counts": counts},
        )
        approval_store.append(run_dir, triage_approval)
        _finish_job(run_dir, approval_id, "amend", "done", f"revalidated {len(result['triage'])} node(s), awaiting triage decision")
    except Exception as exc:  # noqa: BLE001
        _finish_job(run_dir, approval_id, "amend", "failed", str(exc))


def _run_triage_job(run_dir: Path, approval_id: str) -> None:
    approval = _find_approval(run_dir, approval_id)
    if approval is None:
        _finish_job(run_dir, approval_id, "triage", "failed", "approval record missing")
        return
    context = approval.context or {}
    try:
        options, provider, env, factory = _runtime_for(run_dir)
        repaired = asyncio_run(apply_triage(run_dir, triage=context.get("triage", {}), writer_adapter_factory=factory, env=env, provider=provider, max_attempts=options.max_attempts))
        _finish_job(run_dir, approval_id, "triage", "done", f"repaired: {', '.join(repaired) if repaired else '(none)'}")
    except Exception as exc:  # noqa: BLE001
        _finish_job(run_dir, approval_id, "triage", "failed", str(exc))


def _run_reopen_job(run_dir: Path, approval_id: str) -> None:
    approval = _find_approval(run_dir, approval_id)
    if approval is None:
        _finish_job(run_dir, approval_id, "reopen", "failed", "approval record missing")
        return
    context = approval.context or {}
    try:
        options, provider, env, factory = _runtime_for(run_dir)
        news = asyncio_run(reopen_node(run_dir, node_id=context.get("node_id", ""), defect=context.get("defect", ""), writer_adapter_factory=factory, env=env, provider=provider, max_attempts=options.max_attempts))
        _finish_job(run_dir, approval_id, "reopen", "done", f"repaired: {', '.join(news)}")
    except Exception as exc:  # noqa: BLE001
        _finish_job(run_dir, approval_id, "reopen", "failed", str(exc))


def _runtime_for(run_dir: Path):
    from ..environment.local import LocalEnvironment
    from ..v1.provider import OpenAICompatibleProvider
    from ..pipeline.backends import build_writer_adapter

    spec = _read_json(run_spec_path(run_dir)) or {}
    options = RunOptions.from_spec(spec)
    provider = OpenAICompatibleProvider(model=options.model)
    env = LocalEnvironment(tmp_dir=str(run_dir / "tmp"))
    factory = lambda node: build_writer_adapter(  # noqa: E731
        options.backend,
        workspace_path=run_dir,
        prompt_dir=run_dir / "tmp" / "prompts",
        node=node,
        model=options.model,
    )
    return options, provider, env, factory


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


def _job_thread(run_dir: Path, kind: str, job_id: str, target: Any, **kwargs: Any) -> None:
    try:
        target(run_dir, **kwargs)
    except Exception as exc:  # noqa: BLE001
        _finish_job(run_dir, job_id, kind, "failed", str(exc))


def _finish_job(run_dir: Path, job_id: str, kind: str, status: str, detail: str) -> None:
    _append_job(run_dir, {"job_id": job_id, "kind": kind, "status": status, "ts": _now(), "detail": detail})


def _append_job(run_dir: Path, record: dict[str, Any]) -> None:
    path = jobs_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _read_jobs(run_dir: Path) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in _read_jsonl(jobs_path(run_dir)):
        job_id = record.get("job_id")
        if not job_id:
            continue
        if job_id not in merged:
            order.append(job_id)
        merged[job_id] = record
    return [merged[key] for key in order]


def _find_approval(run_dir: Path, approval_id: str) -> approval_store.Approval | None:
    for item in approval_store.read_all(run_dir):
        if item.approval_id == approval_id:
            return item
    return None


def _load_tree(run_dir: Path) -> TaskTree:
    try:
        return TaskTree.load(run_dir / "tree.json")
    except (OSError, ValueError):
        return TaskTree(nodes={})


def _tree_summary(tree: TaskTree) -> list[dict[str, Any]]:
    return [
        {
            "id": node.id,
            "brief": node.brief,
            "status": node.status,
            "shape": node.shape,
            "artifact": node.artifact,
            "judgment": len(node.judgment),
            "gates": len(node.gates),
            "attempts": node.attempts,
            "depends_on": node.depends_on,
        }
        for node in tree.nodes.values()
    ]


def _count_statuses(tree: TaskTree) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in tree.nodes.values():
        counts[node.status] = counts.get(node.status, 0) + 1
    return counts


def _phase_map(events: list[dict[str, Any]]) -> dict[str, str]:
    phases: dict[str, str] = {}
    for event in events:
        if event.get("type") == "phase_started":
            phases[event.get("phase", "")] = "in_progress"
        elif event.get("type") == "phase_done":
            phases[event.get("phase", "")] = event.get("status", "done")
        elif event.get("type") == "phase_failed":
            phases[event.get("phase", "")] = "error"
    return phases


def _parse_plan_payload(raw: Any) -> dict[str, Any]:
    from ..pipeline.backends import parse_research_plan

    if not raw:
        return {}
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        return parse_research_plan(raw)
    except (ValueError, json.JSONDecodeError):
        return {}


def _safe_node_id(value: str) -> bool:
    return bool(value) and "/" not in value and "\\" not in value and value not in {".", ".."}


def _input_tokens(run_dir: Path, ref: str) -> int:
    path = Path(ref)
    if not path.is_absolute():
        return 0
    return estimate_tokens(_read_text(path) or "")


def _input_exists(run_dir: Path, ref: str) -> bool:
    path = Path(ref)
    if path.is_absolute():
        return path.exists()
    return (run_dir / ref).exists()


def _list_versions(run_dir: Path, node_id: str) -> list[str]:
    directory = versions_dir(run_dir, node_id)
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_file())


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _read_json(path: Path) -> Any:
    raw = _read_text(path)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    raw = _read_text(path)
    records: list[dict[str, Any]] = []
    if not raw:
        return records
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            records.append(data)
    return records


def _has_content(path: Path) -> bool:
    text = _read_text(path)
    return bool(text and text.strip())


def _dir_mtime(directory: Path) -> float:
    try:
        return max(
            (p.stat().st_mtime for p in directory.rglob("*") if p.is_file()),
            default=0.0,
        )
    except OSError:
        return 0.0