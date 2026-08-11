"""Run-directory state for the web dashboard (PLAN.md §11).

Formerly ``tui/state.py`` (2026-08-09: the Textual TUI was itself replaced
by this web app the same day it was deleted — see CLAUDE.md's v5 section
for the full back-and-forth). Moved here unchanged in spirit: still the
same "read everything fresh from disk on every call" design, still no
``control_enabled``/403 concept on *this* class — that gate now lives one
layer up, in ``dashboard/server.py``'s HTTP handler, since a web server
(unlike a TUI or a bound terminal) may be reachable from more than just
the operator's own machine. The "subagents" view and live mid-episode
messaging the TUI added over the original dashboard state are kept as-is.

Bridges three worlds without coupling them:

* the pipeline driver (hosted in a background thread by this same process,
  or running in a detached ``kusudaemon run`` process — this state never
  tells the difference, because the only contract is the run directory),
* the dashboard server's render loop (snapshots on every request, or every
  SSE tick),
* the operator (approve/amend/reopen/halt/interject actions, over HTTP).

The approval protocol is still ``pipeline/approvals.py``'s: the driver
creates ``pending`` records in ``approvals.jsonl`` and polls until a
``resolved`` record lands; this state resolves by appending that record.
Any other surface — a second terminal running ``kusudaemon approve`` —
resolves the same file, so no surface owns a run.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..v0.events import EventLog
from ..v0.run_dir import events_path, manifest_path, node_artifact_path, node_scratch_dir, node_trace_path, spec_path
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
from ..pipeline.liveness import run_liveness
from ..pipeline.run_dir import (
    approvals_path,
    halt_path,
    jobs_path,
    phase_path,
    resolve_runs_root,
    resolve_stored,
    run_spec_path,
)
from . import gptme_queue

_DEFAULT_RUN_ID_PREFIX = "rec"

# §11.10.15: the process-lifetime file cache is bounded — a dashboard
# server watches many runs for days, and one entry per file per run must
# not become one entry per hour of run activity.
_CACHE_MAX_ENTRIES = 256


def _now() -> float:
    return time.time()


class RunState:
    """Per-process store for the dashboard server. Thread-safe: the driver,
    job workers, and the server's own request/SSE handlers all touch this
    concurrently."""

    def __init__(self, runs_root: str | Path | None = None) -> None:
        # §D0b: resolved once, here — a `serve` process started from a
        # different cwd than the driver that owns the run must still land
        # on the same absolute directory, or every node reads as empty
        # with nothing in the logs to say why.
        self.runs_root = resolve_runs_root(runs_root) if runs_root else None
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._attached: str | None = None
        self._hosts: dict[str, threading.Thread] = {}
        # Parse-on-change cache for the per-snapshot file reads: keyed by
        # path, value is (stat stamp, parsed result). Depends on the
        # append-only invariant — see _cached_read. Bounded (§11.10.15) and
        # guarded by _cache_lock.
        self._file_cache: dict[str, tuple[Any, Any]] = {}

    def _cached_read(self, path: Path, loader: Callable[[], Any]) -> Any:
        """``loader()`` result cached until the file's (st_size, st_mtime_ns)
        changes — the dashboard's hot path (a snapshot every _STREAM_INTERVAL
        per client) stops re-parsing files that haven't moved
        (PLAN-zeromem.md §10.2).

        §11.10.15: bounded at ``_CACHE_MAX_ENTRIES`` (FIFO eviction — a
        server meant to run for days is a process, not a garbage collector),
        and the dict is mutated only under ``self._cache_lock``: the loader
        itself runs unlocked so concurrent snapshot polls don't serialize
        their parsing behind each other, but the two dict accesses both
        hold the lock.

        **Depends on the append-only invariant**: events.jsonl and
        approvals.jsonl are append-only and fsync'd per record
        (v0/events.py's whole contract), so a same-size same-nanosecond
        rewrite can never occur and the cache cannot serve stale data for
        them. tree.json *is* rewritten in place by ``TaskTree.save``; a
        same-size same-nanosecond rewrite would be served stale for at most
        one poll, accepted per §10.2's stated caveat. If anything ever
        rewrites an append-only log in place, this breaks silently.
        """
        key = str(path)
        try:
            stat = os.stat(path)
            stamp = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            stamp = None
        with self._cache_lock:
            cached = self._file_cache.get(key)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        value = loader()
        with self._cache_lock:
            current = self._file_cache.get(key)
            if current is not None and current[0] != stamp:
                # A concurrent writer landed a fresher entry while we were
                # parsing — leave it; ours is already stale.
                return value
            if len(self._file_cache) >= _CACHE_MAX_ENTRIES:
                del self._file_cache[next(iter(self._file_cache))]
            self._file_cache[key] = (stamp, value)
        return value

    def _cached_events(self, run_dir: Path) -> list[dict[str, Any]]:
        path = events_path(run_dir)
        return self._cached_read(path, lambda: EventLog(path).read_all())

    def _cached_tree(self, run_dir: Path) -> TaskTree:
        return self._cached_read(run_dir / "tree.json", lambda: _load_tree(run_dir))

    def _cached_approvals(self, run_dir: Path) -> list[Any]:
        path = approvals_path(run_dir)
        return self._cached_read(path, lambda: approval_store.read_all(run_dir))

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
                    "hosted": self.is_hosted(entry.name),
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

    def delete_run(self, run_id: str) -> bool:
        if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
            return False
        if self.runs_root is None:
            return False
        run_dir = (self.runs_root / run_id).resolve()
        try:
            run_dir.relative_to(self.runs_root.resolve())
        except ValueError:
            return False
        if not run_dir.is_dir():
            return False
        import shutil

        with self._lock:
            if self._attached == run_id:
                self._attached = None
            shutil.rmtree(run_dir, ignore_errors=True)
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
            return {
                "attached": False,
                "runs": self.list_runs(),
                "server_time": _now(),
            }
        spec = _read_json(run_spec_path(run_dir)) or {}
        goal = str(spec.get("goal", "")).strip()
        if not goal:
            spec_md = _read_text(spec_path(run_dir)) or ""
            if "## Goal" in spec_md:
                try:
                    goal = spec_md.split("## Goal", 1)[1].split("##", 1)[0].strip()
                except Exception:
                    pass
        phase = _read_json(phase_path(run_dir)) or {}
        events = self._cached_events(run_dir)
        tree = self._cached_tree(run_dir)
        approvals = self._cached_approvals(run_dir)
        # §D0c: a phase reading "in_progress" is otherwise indistinguishable
        # from a process that died mid-call -- surface that distinction
        # instead of a permanent, silent "running" badge.
        liveness = run_liveness(run_dir)
        return {
            "attached": True,
            "run_id": self.attached_run_id,
            "runs": self.list_runs(),
            "goal": goal,
            "backend": str(spec.get("backend", "")),
            "phase": str(phase.get("phase", "")),
            "phase_status": str(phase.get("status", "")),
            "phase_detail": str(phase.get("detail", "")),
            "stalled": liveness.stalled,
            "stalled_reason": liveness.reason if liveness.stalled else "",
            "phases": _phase_map(events),
            "tree": _tree_summary(run_dir, tree),
            "tree_counts": _count_statuses(tree),
            "approvals": [item.to_dict() for item in approvals],
            "pending_approvals": [item.to_dict() for item in approvals if item.status == "pending"],
            "events": [e for e in events[-200:]],
            "events_count": len(events),
            "subagents": self.subagents(events=events),
            "jobs": _read_jobs(run_dir),
            "halted": halt_path(run_dir).exists(),
            "hosted": self.is_hosted(),
            "has_spec": _has_content(spec_path(run_dir)),
            "has_contract": contract_path(run_dir).exists(),
            "has_assembly": assembly_output_path(run_dir).exists(),
            "server_time": _now(),
        }

    def events_tail(self, after: int = 0) -> list[dict[str, Any]]:
        run_dir = self._attached_dir()
        if run_dir is None:
            return []
        events = self._cached_events(run_dir)
        return [e for e in events[after:]]

    # ------------------------------------------------------------------
    # Subagents: every distinct dispatched episode (tree Writer nodes,
    # repairs, research queries, pilot drafts) seen in events.jsonl -- the
    # harness's own vocabulary for "subagent" (see PLAN.md/CLAUDE.md's v4
    # section). Each one funnels through the same GptmeAdapter/
    # _gptme_worker.py, so the same live-trace/interject mechanism covers
    # all of them uniformly.
    # ------------------------------------------------------------------
    def subagents(self, *, events: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        run_dir = self._attached_dir()
        if run_dir is None:
            return []
        if events is None:
            events = EventLog(events_path(run_dir)).read_all()
        by_node: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for event in events:
            node_id = event.get("node_id")
            if not node_id or node_id == "-":
                continue
            if node_id not in by_node:
                order.append(node_id)
                by_node[node_id] = []
            by_node[node_id].append(event)
        result: list[dict[str, Any]] = []
        for node_id in order:
            node_events = by_node[node_id]
            result.append(_summarize_subagent(run_dir, node_id, node_events))
        return result

    def node_gptme_logdir(self, node_id: str) -> Path | None:
        """Scan a node's trace.jsonl for the ``{"type": "logdir", ...}``
        line ``_gptme_worker.py`` prints before ``gptme.chat()`` starts.
        If node_id is "main", "root", or "harness", checks current active phase,
        live subagents, or the latest trace in the run's traces directory."""
        run_dir = self._attached_dir()
        if run_dir is None or not _safe_node_id(node_id):
            return None
        direct = _last_logdir(node_trace_path(run_dir, node_id))
        if direct is not None:
            return direct

        if node_id in {"main", "root", "harness"}:
            snap = self.snapshot()
            current_phase = snap.get("phase")
            if current_phase:
                phase_logdir = _last_logdir(node_trace_path(run_dir, current_phase))
                if phase_logdir is not None:
                    return phase_logdir
            subagents = snap.get("subagents") or []
            for sub in subagents:
                if sub.get("live"):
                    sub_id = sub.get("id")
                    if sub_id:
                        sub_log = _last_logdir(node_trace_path(run_dir, sub_id))
                        if sub_log is not None:
                            return sub_log
            traces_dir = run_dir / "traces"
            if traces_dir.exists():
                trace_files = sorted(traces_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                for tf in trace_files:
                    found = _last_logdir(tf)
                    if found is not None:
                        return found

        return None

    def interject(self, node_id: str, text: str) -> bool:
        """Send a live message into a currently-running subagent's gptme
        session — appends to that session's ``prompt-queue.jsonl``, which
        gptme's own chat loop drains between turns (see
        ``dashboard/gptme_queue.py``). Returns False if no live session has been
        discovered for this node yet (nothing running, or too early)."""
        text = (text or "").strip()
        if not text:
            return False
        logdir = self.node_gptme_logdir(node_id)
        if logdir is None:
            return False
        gptme_queue.queue_prompt(logdir, text)
        return True

    # ------------------------------------------------------------------
    # Hosted runs (hosted state drives the driver in a background thread)
    # ------------------------------------------------------------------
    def start_run(self, body: dict[str, Any], *, driver=None) -> tuple[str | None, str]:
        """Create and start a recursive run, or resume an existing one.
        Returns (run_id, error). Reusing an already-dispatched ``run_id`` is
        a resume (§10/§13: "resume is exactly run with an existing run-id");
        in that case ``run.spec.json`` on disk — not this call's body — is
        authoritative for goal/source/backend/etc, the same rule
        ``pipeline/run.py``'s ``run_from_args`` already applies for the CLI
        path. Only ``run_id`` from the body is used for a resume."""
        if self.runs_root is None:
            return None, "no runs_root configured"
        run_id = str(body.get("run_id") or "").strip()
        if run_id and ("/" in run_id or "\\" in run_id or run_id in {".", ".."}):
            return None, "invalid run_id"
        run_dir = self.runs_root / run_id if run_id else None
        resuming = run_dir is not None and run_spec_path(run_dir).exists()
        if resuming:
            options = RunOptions.from_spec(_read_json(run_spec_path(run_dir)) or {})
        else:
            goal = str(body.get("goal", "")).strip()
            if not goal:
                return None, "goal is required"
            if not run_id:
                run_id = f"{_DEFAULT_RUN_ID_PREFIX}{int(_now())}{uuid.uuid4().hex[:6]}"
            run_dir = self.runs_root / run_id
            options = self._options_from_body(body, goal)
        from ..v0.run_dir import create_run_dir

        create_run_dir(self.runs_root, run_id)
        if driver is None:
            driver = self._default_driver(run_dir, options)
        thread = threading.Thread(
            target=_host_driver, args=(run_dir, driver), name=f"kusudaemon-dashboard-{run_id}", daemon=True
        )
        with self._lock:
            self._hosts[run_id] = thread
            self._attached = run_id
        thread.start()
        return run_id, ""

    @staticmethod
    def _options_from_body(body: dict[str, Any], goal: str) -> RunOptions:
        from ..pipeline.run import _read_text_arg

        return RunOptions(
            goal=_read_text_arg(goal),
            backend=str(body.get("backend") or _default_backend()),
            model=body.get("model") or None,
            source_text=_read_text_arg(str(body.get("source", ""))),
            compile_command=body.get("compile_command") or None,
            research_plan=_parse_plan_payload(body.get("research_plan")),
            max_rounds=int(body.get("max_rounds", 100)),
            max_attempts=int(body.get("max_attempts", 3)),
        )

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
        artifact = _read_text(node_artifact_path(run_dir, node_id)) or ""
        from ..v1.gates import evaluate_gates, read_gate_cache

        # §11.10.11: gates were evaluated once, at dispatch, and cached in
        # audit/<node>.json — read that instead of re-evaluating on every
        # poll. Fall back to a live evaluation only when no dispatch has
        # cached anything yet.
        cached = read_gate_cache(run_dir / "audit" / f"{node_id}.json")
        if cached is None:
            cached = [
                {"gate": result.gate, "passed": result.passed, "detail": result.detail}
                for result in evaluate_gates(node.gates, artifact)
            ]
        gate_results = cached
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
        text = _read_text(node_trace_path(run_dir, node_id))
        if text and text.strip():
            return text
        if node_id in {"main", "root", "harness"}:
            snap = self.snapshot()
            current_phase = snap.get("phase")
            if current_phase:
                phase_text = _read_text(node_trace_path(run_dir, current_phase))
                if phase_text and phase_text.strip():
                    return phase_text
            subagents = snap.get("subagents") or []
            for sub in subagents:
                if sub.get("live"):
                    sub_id = sub.get("id")
                    if sub_id:
                        sub_text = _read_text(node_trace_path(run_dir, sub_id))
                        if sub_text and sub_text.strip():
                            return sub_text
            traces_dir = run_dir / "traces"
            if traces_dir.exists():
                trace_files = sorted(traces_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                for tf in trace_files:
                    txt = _read_text(tf)
                    if txt and txt.strip():
                        return txt
        return text

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

    return os.getenv("KUSUDAEMON_BACKEND", "gptme")


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


def _tree_summary(run_dir: Path, tree: TaskTree) -> list[dict[str, Any]]:
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
            "artifact_count": _artifact_count(run_dir, node.id),
        }
        for node in tree.nodes.values()
    ]


def _artifact_count(run_dir: Path, node_id: str) -> int:
    """How many artifact files this node has produced: the current
    ``out/<node>.md`` (if non-empty) plus every pre-repair snapshot under
    ``out/.versions/<node>/`` -- shown on each Task Tree row per the
    dashboard's node-level artifact count."""
    count = 1 if _has_content(node_artifact_path(run_dir, node_id)) else 0
    count += len(_list_versions(run_dir, node_id))
    return count


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
    # §D0b: a planner-built node's inputs are stored relative to run_dir
    # (v2/survey.py:unit_input_path), so the old `not absolute -> 0` branch
    # under-reported every one of them as zero input tokens.
    return estimate_tokens(_read_text(resolve_stored(run_dir, ref)) or "")


def _input_exists(run_dir: Path, ref: str) -> bool:
    return resolve_stored(run_dir, ref).exists()


def _list_versions(run_dir: Path, node_id: str) -> list[str]:
    directory = versions_dir(run_dir, node_id)
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_file())


def _kind_of(node_id: str) -> str:
    if "~repair" in node_id:
        return "repair"
    if "~research~" in node_id:
        return "research"
    return "writer"


def _summarize_subagent(run_dir: Path, node_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    role = "writer"
    status = "pending"
    duration_ms = 0
    error: str | None = None
    attempts = 0
    completed = False
    for event in events:
        etype = event.get("type")
        role = event.get("role", role)
        if etype in ("node_dispatched", "node_redispatched"):
            attempts += 1
            status = "running"
        elif etype == "session_captured":
            status = "running"
        elif etype == "episode_completed":
            completed = True
            status = str(event.get("status", "done"))
            duration_ms = int(event.get("duration_ms") or 0)
            error = event.get("error")
    logdir = _last_logdir(node_trace_path(run_dir, node_id))
    live = bool(logdir) and not completed
    return {
        "id": node_id,
        "kind": _kind_of(node_id),
        "role": role,
        "status": status,
        "attempts": attempts,
        "duration_ms": duration_ms,
        "error": error,
        "live": live,
        "has_logdir": logdir is not None,
    }


def _last_logdir(trace_path: Path) -> Path | None:
    raw = _read_text(trace_path)
    if not raw:
        return None
    found: str | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "logdir" and record.get("logdir"):
            found = str(record["logdir"])
    return Path(found) if found else None


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
