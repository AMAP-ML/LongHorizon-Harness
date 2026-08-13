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
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..v0.events import EventLog
from ..v0.run_dir import events_path, manifest_path, node_artifact_path, node_scratch_dir, node_trace_path, spec_path, write_text_atomic
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
    driver_pid_path,
    halt_path,
    jobs_path,
    phase_path,
    resolve_runs_root,
    resolve_stored,
    run_spec_path,
    tier_path,
    tree_path,
)
from . import gptme_queue
from . import rendering
from .rendering import TraceEntry

_DEFAULT_RUN_ID_PREFIX = "rec"


def _available_models_and_default() -> tuple[list[str], str]:
    try:
        from ..provider_config import list_available_models, resolve
        models = list_available_models()
        default_m = ""
        try:
            default_m = resolve().model
        except Exception:
            if models:
                default_m = models[0]
        return models, default_m
    except Exception:
        return [], ""


def _provider_config_stat_path() -> Path | None:
    """§E20e (2026-08-12 audit): the file whose stat stamp gates the cached
    models/default-model lookup below. ``config_file_path()`` always
    returns *some* path (falling back to the plain cwd-relative name even
    when nothing exists yet on disk, per its own docstring), so a missing
    file still gets a stable, stat-able key -- ``os.stat`` on it simply
    raises, ``_cached_read`` treats that as a ``None`` stamp, and the
    lookup only re-runs once the file actually appears."""
    try:
        from ..provider_config import config_file_path
        return config_file_path()
    except Exception:
        return None


# §11.10.15: the process-lifetime file cache is bounded — a dashboard
# server watches many runs for days, and one entry per file per run must
# not become one entry per hour of run activity.
_CACHE_MAX_ENTRIES = 256


@dataclass
class _TraceCacheEntry:
    """Incremental-parse state for one trace.jsonl file — see
    ``RunState._parse_trace_incremental``."""

    offset: int = 0
    entries: list[TraceEntry] = field(default_factory=list)
    file_state: dict[str, str] = field(default_factory=dict)


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
        # §DASHBOARD-UX: per-job cancel events. A running job's thread may
        # be mid-provider-call (uninterruptible), but the cancel *record*
        # must land regardless — jobs.jsonl's latest-record-wins merge is
        # the UI's authority, and the event stops the thread's own final
        # status from clobbering the cancel.
        # §E20d (2026-08-12 audit): entries are popped by ``_job_thread``'s
        # ``on_done`` callback once a job reaches a terminal state (done,
        # failed, or cancelled) -- see ``_spawn_job``/``_remove_job_cancel_
        # event``. Without that, this dict only ever grew across every job
        # a long-lived ``serve`` process ever ran.
        self._job_cancel_events: dict[str, threading.Event] = {}
        # Parse-on-change cache for the per-snapshot file reads: keyed by
        # path, value is (stat stamp, parsed result). Depends on the
        # append-only invariant — see _cached_read. Bounded (§11.10.15) and
        # guarded by _cache_lock.
        self._file_cache: dict[str, tuple[Any, Any]] = {}
        # §PERF: incremental trace-parse cache, keyed by resolved trace file
        # path -> _TraceCacheEntry (bytes-consumed offset, accumulated
        # TraceEntry list, accumulated file_state for cross-turn diffing).
        # trace.jsonl is append-only (same invariant _cached_read documents
        # for events.jsonl), so re-parsing only the bytes appended since the
        # last call is safe and turns an O(total trace size) cost the
        # dashboard was paying on *every* poll (every ~1.5s, for as long as
        # an episode runs) into O(bytes appended since the last poll).
        # Bounded and locked the same way as ``_file_cache``.
        self._trace_cache: dict[str, "_TraceCacheEntry"] = {}

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

    def _cached_dir_mtime(self, run_dir: Path) -> float:
        """§E20e (2026-08-12 audit): ``list_runs()`` calls this once per run
        directory under ``runs_root``, on *every* ``snapshot()`` call — not
        just for the attached run, for all of them, every SSE tick. The
        underlying ``_dir_mtime`` is a full ``rglob("*")`` over every file
        in the run (chunks, spine units, every node's ``out``/``scratch``/
        ``audit``/trace files), purely to find a "most recently active"
        sort key. Cached via ``_cached_read``, keyed on the run directory's
        *own* stat stamp — the same "accepted staleness" caveat
        ``_cached_tree`` already documents for ``tree.json`` applies here
        too, slightly wider: a write to a file nested inside e.g.
        ``out/<node>.md`` doesn't bump the top-level run directory's own
        mtime, so the cached value only refreshes when something is
        created/removed directly inside the run dir (which still happens
        repeatedly as a run progresses — ``tree.json``, ``contract.md``,
        ``audit/``, ``out/`` etc. each get created once). This is a display
        sort key, not correctness-bearing state, so that tradeoff is fine."""
        return self._cached_read(run_dir, lambda: _dir_mtime(run_dir))

    def _cached_models_and_default(self) -> tuple[list[str], str]:
        """§E20e (2026-08-12 audit): ``list_available_models()``/``resolve()``
        each independently call ``provider_config.read_config_file()``,
        which re-reads and re-JSON-parses ``provider.json`` from scratch —
        previously done on *every* ``snapshot()`` call, i.e. every
        ``_STREAM_INTERVAL`` tick per connected SSE client, forever, even
        though the file almost never changes mid-run. Cached via the same
        stat-stamp ``_cached_read`` pattern every other per-snapshot disk
        read in this file already uses, keyed on the resolved config path
        itself so a config file that appears/changes/disappears is picked
        up on the next tick."""
        path = _provider_config_stat_path()
        if path is None:
            return _available_models_and_default()
        return self._cached_read(path, _available_models_and_default)

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
            # §DASHBOARD-UX §3: a run waiting on the operator must read as
            # one at a glance from the rail's run chips — count pending
            # approvals per run (cached like every other per-snapshot read).
            approvals = self._cached_approvals(entry)
            runs.append(
                {
                    "id": entry.name,
                    "goal": str(spec.get("goal", "")),
                    "phase": str(phase.get("phase", "")),
                    "status": str(phase.get("status", "created")),
                    "detail": str(phase.get("detail", "")),
                    "mtime": self._cached_dir_mtime(entry),
                    "attached": entry.name == self._attached,
                    "hosted": self.is_hosted(entry.name),
                    "pending_approvals": sum(1 for item in approvals if item.status == "pending"),
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

    def set_model_override(self, model: str | None) -> bool:
        run_dir = self._attached_dir()
        if run_dir is None:
            return False
        override_file = run_dir / "model_override.json"
        if model is None or not model.strip():
            try:
                override_file.unlink()
            except OSError:
                pass
        else:
            write_text_atomic(
                override_file,
                json.dumps({"model": model.strip(), "ts": _now()}, indent=2) + "\n",
            )
        return True

    def get_model_override(self) -> str | None:
        run_dir = self._attached_dir()
        if run_dir is None:
            return None
        override_file = run_dir / "model_override.json"
        data = _read_json(override_file)
        if isinstance(data, dict):
            return data.get("model")
        return None

    def set_tier_override(self, tier: str | None) -> bool:
        run_dir = self._attached_dir()
        if run_dir is None:
            return False
        rec = _read_json(tier_path(run_dir)) or {}
        rec["override"] = tier
        write_text_atomic(tier_path(run_dir), json.dumps(rec, indent=2) + "\n")
        return True

    def reopen_node(self, node_id: str, defect: str = "") -> bool:
        run_dir = self._attached_dir()
        if run_dir is None:
            return False
        tree_file = tree_path(run_dir)
        if not tree_file.is_file():
            return False
        tree = TaskTree.load(tree_file)
        node = tree.nodes.get(node_id)
        if node is None:
            return False
        node.status = "pending"
        node.last_defect = defect
        tree.save(tree_file)
        EventLog(events_path(run_dir)).append({"type": "node_reopened", "node_id": node_id, "defect": defect, "ts": _now()})
        return True

    # ------------------------------------------------------------------
    # Snapshots (always read fresh; the disk is authoritative)
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        models, default_model = self._cached_models_and_default()
        run_dir = self._attached_dir()
        if run_dir is None:
            return {
                "attached": False,
                "runs": self.list_runs(),
                "models": models,
                "default_model": default_model,
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
        # §C4: tier + escalation history belong in the run header, per
        # PLAN.md §C4's last paragraph. tier.json holds the current
        # ``{tier, measured_tier, override, ...}``; the escalation history
        # is derived from events.jsonl's ``run_tier_escalated`` rows.
        tier_record, escalation_history = self._tier_and_escalation(run_dir, events)
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
            "tier": tier_record.get("tier", ""),
            "measured_tier": tier_record.get("measured_tier", ""),
            "tier_override": tier_record.get("override"),
            "escalation_history": escalation_history,
            "phases": _phase_map(events),
            "tree": _tree_summary(run_dir, tree, cached_read=self._cached_read),
            "tree_counts": _count_statuses(tree),
            "elapsed": (_now() - (events[0].get("ts") or _now())) if events else 0.0,
            "hosted_count": self.hosted_count(),
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
            "models": models,
            "default_model": default_model,
            "model_override": self.get_model_override(),
            "server_time": _now(),
        }

    def events_tail(self, after: int = 0) -> list[dict[str, Any]]:
        run_dir = self._attached_dir()
        if run_dir is None:
            return []
        events = self._cached_events(run_dir)
        return [e for e in events[after:]]

    # ------------------------------------------------------------------
    # §C4: tier + escalation history for the run header. Read fresh off
    # disk on every snapshot call, like every other field in this method —
    # tier.json changes when an escalation trigger fires mid-run, and the
    # dashboard needs to surface that without restart.
    # ------------------------------------------------------------------
    def _tier_and_escalation(
        self, run_dir: Path, events: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        tier_record: dict[str, Any] = {}
        tp = tier_path(run_dir)
        if tp.exists():
            try:
                tier_record = json.loads(tp.read_text(encoding="utf-8"))
                if not isinstance(tier_record, dict):
                    tier_record = {}
            except (OSError, json.JSONDecodeError):
                tier_record = {}
        escalation_history: list[dict[str, Any]] = []
        for event in events:
            if event.get("type") != "run_tier_escalated":
                continue
            escalation_history.append(
                {
                    "trigger": str(event.get("trigger", "")),
                    "from": str(event.get("from", "")),
                    "to": str(event.get("to", "")),
                    "node_id": str(event.get("node_id", "")) if event.get("node_id") else "",
                    "ts": int(event.get("ts", 0) or 0),
                }
            )
        return tier_record, escalation_history

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
            result.append(_summarize_subagent(run_dir, node_id, node_events, logdir_for=self._cached_logdir))
        return result

    def _cached_logdir(self, trace_path: Path) -> Path | None:
        """§PERF: ``_summarize_subagent`` per subagent reads the *whole*
        trace.jsonl just to find its logdir line — with append-only traces
        that full read is cacheable the same way ``_cached_events`` is
        (this is the dominant cost of every snapshot on a run whose
        episodes have grown large traces)."""
        return self._cached_read(trace_path, lambda: _last_logdir(trace_path))

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
            # §E20g (2026-08-12 audit): this used to call ``self.snapshot()``
            # just to read ``phase`` and ``subagents`` back out of it — a
            # full snapshot build (tree summary, tier/escalation history,
            # models lookup, the whole run list) for a lookup that only
            # needs two narrow pieces of state. Read those directly instead.
            current_phase = str((_read_json(phase_path(run_dir)) or {}).get("phase", ""))
            if current_phase:
                phase_logdir = _last_logdir(node_trace_path(run_dir, current_phase))
                if phase_logdir is not None:
                    return phase_logdir
            for sub in self.subagents(events=self._cached_events(run_dir)):
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
        workspace_root: Path | None = None
        if resuming:
            options = RunOptions.from_spec(_read_json(run_spec_path(run_dir)) or {})
            other = _other_driver_pid(run_dir)
            if other is not None:
                return None, other
            flag = halt_path(run_dir)
            try:
                flag.unlink()
            except OSError:
                pass
        else:
            goal = str(body.get("goal", "")).strip()
            if not goal:
                return None, "goal is required"
            if not run_id:
                run_id = f"{_DEFAULT_RUN_ID_PREFIX}{int(_now())}{uuid.uuid4().hex[:6]}"
            run_dir = self.runs_root / run_id
            try:
                # §PERF: `measure_workspace_now=False` — measuring a real
                # workspace reads and token-counts every included file
                # (v6/work_object.py's `_iter_included_files`), which for a
                # real repo can take tens of seconds. Doing that here, in
                # the HTTP request thread, blocked the `POST /api/runs`
                # response (and so the operator's "start run" click) on the
                # full measurement. Only the cheap `is_dir()` validation
                # happens synchronously now; the actual measurement moves
                # into the background host thread below.
                options, workspace_root = self._options_from_body(body, goal, measure_workspace_now=False)
            except (ValueError, OSError) as exc:
                return None, str(exc)
        from ..v0.run_dir import create_run_dir

        create_run_dir(self.runs_root, run_id)
        # §E9 (2026-08-12 audit): _host_driver must remove this run's entry
        # from self._hosts when the driver thread finishes, regardless of
        # success/failure — otherwise hosted_count() grows monotonically
        # across every completed run in a long-lived `serve` process, and
        # snapshot()["hosted"] stays permanently true, which is what made
        # the dashboard's Resume button branch into "un-halt" (a no-op for
        # a genuinely finished/dead run) instead of "re-host". `on_done`
        # is called from _host_driver's own finally block.
        on_done = lambda: self._remove_host(run_id)  # noqa: E731
        if driver is None:
            if workspace_root is not None:
                def _build_and_host(run_dir=run_dir, options=options, workspace_root=workspace_root, on_done=on_done) -> None:
                    from ..v6.work_object import measure_workspace

                    options.work_object = measure_workspace(workspace_root)
                    _host_driver(run_dir, self._default_driver(run_dir, options), on_done=on_done)

                thread = threading.Thread(
                    target=_build_and_host, name=f"kusudaemon-dashboard-{run_id}", daemon=True
                )
            else:
                driver = self._default_driver(run_dir, options)
                thread = threading.Thread(
                    target=_host_driver, args=(run_dir, driver),
                    kwargs={"on_done": on_done},
                    name=f"kusudaemon-dashboard-{run_id}", daemon=True,
                )
        else:
            thread = threading.Thread(
                target=_host_driver, args=(run_dir, driver),
                kwargs={"on_done": on_done},
                name=f"kusudaemon-dashboard-{run_id}", daemon=True,
            )
        with self._lock:
            self._hosts[run_id] = thread
            self._attached = run_id
        thread.start()
        return run_id, ""

    @staticmethod
    def _options_from_body(
        body: dict[str, Any], goal: str, *, measure_workspace_now: bool = True
    ) -> tuple[RunOptions, Path | None]:
        """§DASHBOARD-UX §11: the new-run form is now the full RunOptions
        surface — every option ``pipeline run`` accepts on the CLI, plus
        ``workspace`` (PLAN.md §A3 kind="workspace": a real directory the
        Writer edits directly) and ``tier_override`` (the §A4.4 floor, never
        a ceiling). A bad ``workspace`` path or a non-T0..T3
        ``tier_override`` raises — the route surfaces the message.

        Returns ``(options, workspace_root)``: ``workspace_root`` is the
        validated (but, unless ``measure_workspace_now``, not yet measured)
        workspace path, or ``None`` when no workspace was given — the
        caller's signal for whether measurement still needs to happen."""
        from ..pipeline.run import _read_text_arg

        work_object = None
        workspace_root: Path | None = None
        workspace = str(body.get("workspace") or "").strip()
        if workspace:
            root = Path(workspace).expanduser().resolve()
            if not root.is_dir():
                raise ValueError(f"workspace path is not a directory: {workspace}")
            workspace_root = root
            if measure_workspace_now:
                from ..v6.work_object import measure_workspace

                work_object = measure_workspace(root)
        # §E7 (2026-08-12 audit): the new-run form's tier-floor select can
        # plausibly send a bare digit ("2"), lowercase ("t2"), or the
        # canonical form ("T2") -- normalize all three to canonical "T<n>"
        # before validating/storing, rather than requiring the literal
        # "T0".."T3" the server used to accept as the only valid spelling.
        raw_tier = str(body.get("tier_override") or body.get("tier_floor") or "").strip()
        tier_override: str | None = None
        if raw_tier:
            normalized_tier = raw_tier.upper()
            if normalized_tier in ("0", "1", "2", "3"):
                normalized_tier = f"T{normalized_tier}"
            if normalized_tier not in ("T0", "T1", "T2", "T3"):
                raise ValueError(f"invalid tier_override: {raw_tier!r} (want T0-T3 or blank)")
            tier_override = normalized_tier

        options = RunOptions(
            goal=_read_text_arg(goal),
            backend=str(body.get("backend") or _default_backend()),
            model=body.get("model") or None,
            source_text=_read_text_arg(str(body.get("source", ""))),
            work_object=work_object,
            compile_command=body.get("compile_command") or None,
            research_plan=_parse_plan_payload(body.get("research_plan")),
            max_rounds=int(body.get("max_rounds", 100)),
            max_attempts=int(body.get("max_attempts", 3)),
            dispatch_policy=str(body.get("dispatch_policy") or "model"),
            max_parallel=int(body.get("max_parallel", 1)),
            document_review=bool(body.get("document_review", False)),
            survey_mode=str(body.get("survey_mode") or "model"),
            inline_spans=bool(body.get("inline_spans", False)),
            tier_override=tier_override,
            auto_probe_plan=bool(body.get("auto_probe_plan", True)),
        )
        return options, workspace_root

    def _default_driver(self, run_dir: Path, options: RunOptions) -> RecursiveDriver:
        from ..v1.provider import OpenAICompatibleProvider, RATE_LIMIT_BACKOFFS

        # §D11: same ``on_backoff`` wiring as ``pipeline/run.py`` — a
        # dashboard-hosted run's provider ladder waits are visible on the
        # same event stream the dashboard already reads.
        log: list[EventLog] = []

        def _on_backoff(attempt: int, delay_s: float) -> None:
            if not log:
                log.append(EventLog(events_path(run_dir)))
            log[0].append(
                {
                    "node_id": "-",
                    "role": "harness",
                    "round": 0,
                    "type": "rate_limit_backoff",
                    "attempt": attempt,
                    "delay_s": delay_s,
                    "rungs": len(RATE_LIMIT_BACKOFFS),
                }
            )

        return RecursiveDriver(
            run_dir,
            provider=OpenAICompatibleProvider(model=options.model, on_backoff=_on_backoff),
            options=options,
        )

    def _remove_host(self, run_id: str) -> None:
        """§E9: called from ``_host_driver``'s ``finally`` block once the
        driver thread finishes (success, failure, or an exception raised
        before ``driver.run()`` was even awaitable) — the counterpart to
        ``start_run``'s ``self._hosts[run_id] = thread``. Without this,
        ``hosted_count()`` only ever grows and ``is_hosted()`` stays True
        forever for a run whose driver has long since exited."""
        with self._lock:
            self._hosts.pop(run_id, None)

    def is_hosted(self, run_id: str | None = None) -> bool:
        with self._lock:
            rid = run_id or self._attached
            thread = self._hosts.get(rid) if rid else None
            if thread is not None and not thread.is_alive():
                # Defense in depth: a thread that died without reaching
                # _host_driver's finally (shouldn't happen) can't
                # permanently pin the hosted flag either.
                del self._hosts[rid]
                thread = None
            return thread is not None

    def hosted_count(self) -> int:
        """§C4: the number of currently in-flight hosted runs. _post_runs
        checks this against ``max_concurrent_runs`` and returns 429 when
        the cap is reached. Held under the same _lock that mutate-hosts
        operations use, so a concurrent _post_runs sees a consistent
        count rather than reading mid-add.

        §E9: also prunes any entry whose thread has already finished but
        wasn't cleaned up (defense in depth, same rationale as
        ``is_hosted``) so a stray dead entry can't inflate the count
        against ``max_concurrent_runs``."""
        with self._lock:
            dead = [rid for rid, thread in self._hosts.items() if not thread.is_alive()]
            for rid in dead:
                del self._hosts[rid]
            return len(self._hosts)

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

    def kill_run(self, run_id: str | None = None) -> bool:
        """Immediately terminate the driver process for a run (SIGTERM then
        SIGKILL). Writes halt.flag first so the driver won't restart itself,
        then sends the signal to the PID recorded in driver.pid.json. Also
        removes the run from the hosted-threads registry so the capacity cap
        is released. Returns True when the PID was found and signalled (or
        the run was hosted here); False when no driver PID could be located."""

        if self.runs_root is None:
            return False
        target_id = run_id or self.attached_run_id
        if not target_id:
            return False
        if "/" in target_id or "\\" in target_id or target_id in {".", ".."}:
            return False
        run_dir = (self.runs_root / target_id).resolve()
        try:
            run_dir.relative_to(self.runs_root.resolve())
        except ValueError:
            return False
        if not run_dir.is_dir():
            return False

        # Write halt flag first so the driver won't re-enter the next phase.
        try:
            halt_path(run_dir).touch()
        except OSError:
            pass

        # Remove from hosted-threads registry so the capacity cap is released.
        with self._lock:
            self._hosts.pop(target_id, None)

        # Signal the driver process from driver.pid.json.
        pid_file = driver_pid_path(run_dir)
        try:
            record = json.loads(pid_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(record.get("pid"), int):
            return False
        pid: int = record["pid"]
        if pid == os.getpid():
            return True
        from ..utils.process_group import kill_process_group
        kill_process_group(pid, grace_seconds=2.0)
        return True

    # ------------------------------------------------------------------
    # Approval protocol (the disk file is the authority)
    # ------------------------------------------------------------------
    def resolve_approval(
        self,
        approval_id: str,
        *,
        action: str = "",
        user_input: str = "",
        answers: dict[str, str] | None = None,
    ) -> bool:
        run_dir = self._attached_dir()
        if run_dir is None:
            return False
        record = _find_approval(run_dir, approval_id)
        if record is None:
            return False
        # Retry-safe: an approval that is already resolved is a no-op
        # success, not an error. The operator's click can legitimately
        # arrive twice (double-click inside one UI tick, a second browser
        # tab, or the CLI's `approve` racing the browser) — every surface
        # resolves the same record, and every surface after the first must
        # not 409 on a record it never needed to touch.
        if record.status == "resolved":
            return True
        # §DASHBOARD-UX §6.3: a batch-form approval (intake rounds, PLAN.md
        # §A5/§B3) is answered per question — the operator's answers ride
        # in ``answers``, keyed by question id, and the driver reads them
        # back off the resolved record. Every other kind ignores the field
        # exactly the way the CLI's approve path does.
        resolved = record.resolve(action=action, user_input=user_input, answers=answers)
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
        elif kind == "redispatch":
            self._spawn_job(run_dir, "redispatch", _run_redispatch_job, approval.approval_id, approval_id=approval.approval_id)

    def _spawn_job(self, run_dir: Path, kind: str, target: Any, job_id: str, **kwargs: Any) -> None:
        _append_job(run_dir, {"job_id": job_id, "kind": kind, "status": "running", "ts": _now(), "detail": ""})
        cancel_event = threading.Event()
        with self._lock:
            self._job_cancel_events[job_id] = cancel_event
        # §E20d (2026-08-12 audit): the counterpart to §E9's hosted-run
        # cleanup, same pattern -- ``on_done`` runs in ``_job_thread``'s
        # ``finally`` block regardless of how the job ends (done/failed/
        # cancelled, or cancelled-before-start), so a long-lived ``serve``
        # process running many jobs over days doesn't grow
        # ``_job_cancel_events`` forever. Without this, every amend/triage/
        # reopen/redispatch job that ever ran left a permanent entry.
        on_done = lambda: self._remove_job_cancel_event(job_id)  # noqa: E731
        thread = threading.Thread(
            target=_job_thread, args=(run_dir, kind, job_id, target),
            kwargs={**kwargs, "cancel_event": cancel_event, "on_done": on_done}, daemon=True,
        )
        thread.start()

    def _remove_job_cancel_event(self, job_id: str) -> None:
        """§E20d: pop this job's cancel event once its thread is done --
        the counterpart to ``_spawn_job``'s ``self._job_cancel_events[job_id]
        = cancel_event``."""
        with self._lock:
            self._job_cancel_events.pop(job_id, None)

    def cancel_job(self, job_id: str) -> bool:
        """§DASHBOARD-UX §11: cancel a running dashboard job (amend/triage/
        reopen/redispatch). Jobs are daemon threads that may be mid-provider-
        call, so the *record* is the authority: a ``cancelled`` line in
        jobs.jsonl (latest record per job_id wins) and an event flag that
        stops the thread's own final status from clobbering it."""
        run_dir = self._attached_dir()
        if run_dir is None or not job_id:
            return False
        with self._lock:
            event = self._job_cancel_events.get(job_id)
        if event is not None:
            event.set()
        _append_job(
            run_dir,
            {"job_id": job_id, "kind": "", "status": "cancelled", "ts": _now(), "detail": "cancelled by operator"},
        )
        return True

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

    def escalate(self) -> dict[str, Any]:
        """§DASHBOARD-UX §11: the rail tier chip's escalate action — §B2's
        operator intervention, one tier up (invariant 9: tiers only rise),
        the same read-modify-write ``pipeline/escalate`` performs. Raises
        FileNotFoundError/ValueError for the route to surface."""
        run_dir = self._attached_dir()
        if run_dir is None:
            raise FileNotFoundError("no attached run")
        from ..pipeline.driver import escalate_run

        return escalate_run(run_dir)

    def pilot_save(self, approval_id: str, node_id: str, text: str) -> bool:
        """§DASHBOARD-UX §6.2: pilot edit + approve in one browser action.
        Writes the edited text back to the pilot's artifact path (exactly
        what ``approve_pilot`` does anyway) and resolves the pending pilot
        approval carrying the edit as ``user_input`` — the driver's
        ``edited = approval.user_input.strip() or artifact`` line picks it
        up verbatim and derives contract rules from the diff. ``approve
        as-is`` stays the blank-``user_input`` zero-model-call path."""
        run_dir = self._attached_dir()
        if run_dir is None:
            return False
        record = _find_approval(run_dir, approval_id)
        if record is None or record.status == "resolved" or record.kind != "pilot":
            return False
        context_node = str(record.context.get("node_id", ""))
        if context_node and context_node != node_id:
            return False
        if not _safe_node_id(node_id):
            return False
        text = str(text or "")
        node_artifact_path(run_dir, node_id).write_text(text, encoding="utf-8")
        resolved = record.resolve(action="save", user_input=text)
        approval_store.append(run_dir, resolved)
        return True

    def request_redispatch(self, node_id: str, reason: str = "") -> tuple[dict[str, Any] | None, str]:
        """§DASHBOARD-UX §11: re-dispatch a single node that never made it —
        failed/blocked/stale. Approval-gated (a redispatch costs a Writer
        episode, same as reopen); on apply the tree is reset so the round
        loop picks the node up again with a fresh attempt budget. Passed
        nodes go through reopen instead (a redispatch can't touch them).

        Returns (approval_dict, "") on success or (None, reason) on failure."""
        run_dir = self._attached_dir()
        if run_dir is None:
            return None, "no run attached to dashboard"
        if not _safe_node_id(node_id):
            return None, f"invalid node id: {node_id!r}"
        tree = _load_tree(run_dir)
        node = tree.nodes.get(node_id)
        if node is None:
            return None, f"node {node_id!r} not found in tree (may be a synthetic/derived dispatch — use reopen instead)"
        if node.status in ("passed", "split", "dispatched", "awaiting_review"):
            return None, f"node {node_id!r} is {node.status!r} — not redispatchable (use reopen for passed nodes)"
        approval = approval_store.Approval.create(
            "redispatch",
            title=f"Redispatch {node_id}",
            message=(
                f"Status: {node.status} (attempts {node.attempts}). "
                f"Reason: {reason.strip() or '(none given)'}"
            ),
            options=[
                {"value": "apply", "label": "Redispatch", "style": "primary"},
                {"value": "cancel", "label": "Cancel"},
            ],
            allow_input=False,
            context={"node_id": node_id, "reason": reason.strip()},
        )
        approval_store.append(run_dir, approval)
        return approval.to_dict(), ""

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
            # 2026-08-11: a dispatched-but-tree-less id (the survey explorer's
            # ``explore-01``, or a ``<node>~repair~N`` / ``<node>~research~<slug>``
            # derived dispatch) owns episodes but no tree node. Serve a minimal
            # detail from the subagent summary instead of 404'ing — the
            # inspector re-fetches while ``nodeDetail`` is null, so a 404 made
            # it retry on every render (one 404 per tick, logged to the
            # console, while the agent tab sat on "loading…" forever).
            summary = next((s for s in self.subagents() if s["id"] == node_id), None)
            if summary is None:
                return None
            return {
                "id": node_id,
                "brief": "synthetic agent episode — no tree node (explorer / repair / research dispatch)",
                "status": summary.get("status", "dispatched"),
                "attempts": summary.get("attempts", 0),
                "shape": "",
                "gates": [],
                "gate_results": [],
                "judgment": [],
                "rubric": {},
                "inputs": [],
                "budget": {"tokens": 0, "calls": 0},
                "depends_on": [],
                "artifact": "",
                "artifact_tokens": 0,
                "audit": {},
                "manifest": None,
                "versions": [],
                "promotion": "",
                "split_proposal": None,
                "pilot_original": None,
            }
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
        # §DASHBOARD-UX §11: a split proposal (scratch/<id>/split.json, v7)
        # is operator-legible state — the Node tab on a "split" parent must
        # be able to show what was proposed and why it was accepted.
        split_proposal = _read_json(node_scratch_dir(run_dir, node_id) / "split.json")
        # §DASHBOARD-UX §6.2: the pilot editor's frozen "original" pane —
        # the Writer's pre-edit output under out/.versions/<id>/.
        pilot_original = _read_text(versions_dir(run_dir, node_id) / "pilot-original.md")
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
            "truncated": bool(isinstance(audit, dict) and audit.get("truncated", False)),
            "manifest": manifest_lines[-1] if manifest_lines else None,
            "versions": versions,
            "promotion": promotion.get("promotion", ""),
            "split_proposal": split_proposal,
            "pilot_original": pilot_original,
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
        path = self._resolve_trace_path(node_id)
        return _read_text(path) if path is not None else None

    def _resolve_trace_path(self, node_id: str) -> Path | None:
        """Which trace.jsonl a given node id actually reads from — split out
        of the old ``trace()`` so ``trace_entries()`` can key its
        incremental parse cache on the same resolved path. For "main" (no
        node-scoped trace of its own until a subagent actually runs), the
        fallback chases the current phase's pseudo-trace, then a live
        subagent's, then whichever ``traces/*.jsonl`` was written to most
        recently — unchanged from the original ``trace()`` logic, just
        returning a path instead of already-read text."""
        run_dir = self._attached_dir()
        if run_dir is None or not _safe_node_id(node_id):
            return None
        own_path = node_trace_path(run_dir, node_id)
        text = _read_text(own_path)
        if text and text.strip():
            return own_path
        if node_id in {"main", "root", "harness"}:
            # §E20g (2026-08-12 audit): resolving "main" used to call
            # ``self.snapshot()`` — a full snapshot build (tree summary,
            # tier/escalation history, models lookup, the whole run list —
            # everything ``snapshot()`` assembles) just to read back
            # ``phase`` and ``subagents``. Both are available far more
            # narrowly: ``phase.json`` directly, and ``self.subagents()``
            # (fed the already-cached event log, so it's not even a fresh
            # events.jsonl read) — the same data ``mainAgentId()`` on the
            # client side computes purely from ``snap.subagents`` already.
            current_phase = str((_read_json(phase_path(run_dir)) or {}).get("phase", ""))
            if current_phase:
                phase_trace_path = node_trace_path(run_dir, current_phase)
                phase_text = _read_text(phase_trace_path)
                if phase_text and phase_text.strip():
                    return phase_trace_path
        # §F5 (2026-08-12): for any node whose own trace is empty or missing,
        # check whether a live subagent matching this node_id wrote to a
        # logdir-backed trace path instead (the AGY/gptme adapters write
        # their trace via a logdir path, not directly to traces/<id>.jsonl).
        # For non-main nodes this is the same walk the "main" branch already
        # does for live subagents — we just don't restrict it to the
        # {main, root, harness} guard any more.
        for sub in self.subagents(events=self._cached_events(run_dir)):
            sub_id = sub.get("id")
            if not sub_id:
                continue
            # Prefer the subagent that matches the requested node_id; fall
            # back to any live subagent for "main"-like pseudo-ids.
            if sub_id != node_id and node_id not in {"main", "root", "harness"}:
                continue
            # For a non-pseudo node_id, include non-live (completed/blocked)
            # subagents too — so historical trace is shown after a node stops.
            sub_path = node_trace_path(run_dir, sub_id)
            sub_text = _read_text(sub_path)
            if sub_text and sub_text.strip():
                return sub_path
        traces_dir = run_dir / "traces"
        if traces_dir.exists():
            # For "main"-like ids, use the most recently written trace.
            # For specific node ids, only use their own file from traces/.
            if node_id in {"main", "root", "harness"}:
                trace_files = sorted(traces_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                for tf in trace_files:
                    txt = _read_text(tf)
                    if txt and txt.strip():
                        return tf
        return own_path

    def trace_entries(self, node_id: str) -> list[TraceEntry] | None:
        """The "Thinking" tab's data, via the incremental trace-parse cache
        (§PERF): resolves the same path ``trace()`` would read, then parses
        only the bytes appended since the last call instead of re-parsing
        the whole file. Returns ``None`` exactly when ``trace()`` would —
        no run attached, an unsafe node id — so callers can keep the same
        404-vs-200-empty-list distinction."""
        path = self._resolve_trace_path(node_id)
        if path is None:
            return None
        return self._parse_trace_incremental(path)

    def _parse_trace_incremental(self, path: Path) -> list[TraceEntry]:
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            with self._cache_lock:
                self._trace_cache.pop(key, None)
            return []
        with self._cache_lock:
            cached = self._trace_cache.get(key)
        # A smaller file than last time means it was truncated or rewritten
        # in place (not append-only after all, or a fresh episode reusing a
        # stale cache entry after eviction+recreation) -- reparse from
        # scratch rather than trust a file_state/entries prefix that no
        # longer corresponds to what's on disk.
        if cached is None or size < cached.offset:
            cached = _TraceCacheEntry()
        if size > cached.offset:
            with path.open("rb") as fh:
                fh.seek(cached.offset)
                chunk = fh.read()
            if chunk.endswith(b"\n"):
                consumed = cached.offset + len(chunk)
                new_raw = chunk.decode("utf-8", errors="replace")
            else:
                nl = chunk.rfind(b"\n")
                if nl == -1:
                    # No complete line arrived yet this tick -- a torn
                    # trailing line is re-read whole next tick, same rule
                    # pipeline/approvals.py's _ApprovalScanner documents.
                    consumed = cached.offset
                    new_raw = ""
                else:
                    consumed = cached.offset + nl + 1
                    new_raw = chunk[: nl + 1].decode("utf-8", errors="replace")
            if new_raw:
                rendering.parse_trace_lines(new_raw, cached.entries, cached.file_state)
            cached.offset = consumed
        with self._cache_lock:
            if key not in self._trace_cache and len(self._trace_cache) >= _CACHE_MAX_ENTRIES:
                del self._trace_cache[next(iter(self._trace_cache))]
            self._trace_cache[key] = cached
        return list(cached.entries)

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


def _other_driver_pid(run_dir: Path) -> str | None:
    """§2026-08-11: refuse to double-host a run whose driver is demonstrably
    alive on this host. ``driver.pid.json`` is the same record
    ``run_liveness`` reads (§D0c) — a dead driver (the whole point of
    re-hosting) has a dead pid and passes; a live driver (CLI-launched, or
    hosted by another dashboard instance) refuses with a message the
    operator can act on instead of racing two drivers over one run
    directory. Pid reuse is the one false-positive mode, and it only ever
    produces a visible refusal, never a silent double-run."""
    try:
        job = json.loads(driver_pid_path(run_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if job.get("host") != socket.gethostname() or not isinstance(job.get("pid"), int):
        return None
    try:
        os.kill(job["pid"], 0)
    except PermissionError:
        pass
    except OSError:
        return None
    return f"driver already running (pid={job['pid']}) — the run is not dead; un-halt it instead of re-hosting"


def _host_driver(run_dir: Path, driver: RecursiveDriver, *, on_done: Callable[[], None] | None = None) -> None:
    """Runs a driver to completion in this (background) thread.

    §E9 (2026-08-12 audit): ``on_done`` is called in ``finally`` regardless
    of how this returns — success, a caught exception, or (as
    ``HostedCountTest``'s stub driver exercises) ``asyncio.run`` raising
    immediately because ``driver.run()`` wasn't even a coroutine. Before
    this, only ``kill_run`` ever popped ``RunState._hosts``, so a run that
    finished on its own left a permanent phantom entry: ``hosted_count()``
    grew across every completed run in a long-lived ``serve`` process, and
    ``snapshot()["hosted"]`` stayed true forever, which is what made the
    dashboard's Resume button branch into a no-op "un-halt" instead of
    re-hosting a truly finished/dead run."""
    try:
        try:
            import asyncio

            report = asyncio.run(driver.run())
            _set_phase(run_dir, report.phase, report.status, report.detail)
        except Exception as exc:  # noqa: BLE001 — surface into phase.json, not the thread
            _set_phase(run_dir, "error", "error", str(exc))
    finally:
        if on_done is not None:
            on_done()


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


def _run_redispatch_job(run_dir: Path, approval_id: str) -> None:
    """§DASHBOARD-UX §11: apply an approved redispatch — mark the node
    ``pending`` with a fresh attempt budget and a last_defect recording the
    operator's reason, so the round loop's orchestrator offers it up again.
    Pure tree bookkeeping: no provider call, no writer dispatch."""
    approval = _find_approval(run_dir, approval_id)
    if approval is None:
        _finish_job(run_dir, approval_id, "redispatch", "failed", "approval record missing")
        return
    context = approval.context or {}
    node_id = str(context.get("node_id", ""))
    try:
        tree = TaskTree.load(tree_path(run_dir))
        node = tree.nodes.get(node_id)
        if node is None:
            raise KeyError(f"unknown node: {node_id!r}")
        if node.status in ("passed", "split", "dispatched", "awaiting_review"):
            raise ValueError(f"node {node_id!r} is {node.status!r} — not redispatchable")
        node.status = "pending"
        node.attempts = 0
        node.last_defect = f"redispatch requested by operator: {context.get('reason') or '(no reason)'}"
        tree.save(tree_path(run_dir))
        EventLog(events_path(run_dir)).append(
            {
                "node_id": node_id,
                "role": "operator",
                "round": 0,
                "type": "node_redispatch_requested",
                "reason": context.get("reason", ""),
            }
        )
        _finish_job(run_dir, approval_id, "redispatch", "done", f"node {node_id} reset to pending")
    except Exception as exc:  # noqa: BLE001
        _finish_job(run_dir, approval_id, "redispatch", "failed", str(exc))


def _runtime_for(run_dir: Path):
    from ..environment.local import LocalEnvironment
    from ..v1.provider import OpenAICompatibleProvider
    from ..pipeline.backends import build_writer_adapter

    spec = _read_json(run_spec_path(run_dir)) or {}
    options = RunOptions.from_spec(spec)
    provider = OpenAICompatibleProvider(model=options.model)
    env = LocalEnvironment(tmp_dir=str(run_dir / "tmp"))
    work = options.work_object
    workspace_path = (
        work.root
        if work is not None and work.kind == "workspace" and work.root is not None
        else run_dir
    )
    factory = lambda node: build_writer_adapter(  # noqa: E731
        options.backend,
        workspace_path=workspace_path,
        prompt_dir=run_dir / "tmp" / "prompts",
        node=node,
        model=options.model,
        run_dir=run_dir,
    )
    return options, provider, env, factory


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


def _job_thread(
    run_dir: Path, kind: str, job_id: str, target: Any,
    cancel_event: threading.Event | None = None,
    on_done: Callable[[], None] | None = None,
    **kwargs: Any,
) -> None:
    """§DASHBOARD-UX §11: a job's thread honours its cancel event both
    before starting and after the target returns — a mid-provider-call job
    can't be interrupted, but once the call lands, ``cancelled`` must be
    the final record, not the target's own ``done``/``failed`` write.

    §E20d (2026-08-12 audit): ``on_done`` runs in ``finally`` regardless of
    which branch below fires, same "clean up no matter how this returns"
    contract as ``_host_driver``'s own ``on_done`` (§E9) -- it's how
    ``RunState._job_cancel_events`` stops growing across every job a
    long-lived ``serve`` process ever runs."""
    try:
        if cancel_event is not None and cancel_event.is_set():
            _finish_job(run_dir, job_id, kind, "cancelled", "cancelled by operator before start")
            return
        target(run_dir, **kwargs)
        if cancel_event is not None and cancel_event.is_set():
            _finish_job(run_dir, job_id, kind, "cancelled", "cancelled by operator")
    except Exception as exc:  # noqa: BLE001
        if cancel_event is not None and cancel_event.is_set():
            _finish_job(run_dir, job_id, kind, "cancelled", "cancelled by operator")
        else:
            _finish_job(run_dir, job_id, kind, "failed", str(exc))
    finally:
        if on_done is not None:
            on_done()


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


def _tree_summary(run_dir: Path, tree: TaskTree, *, cached_read: Callable[[Path, Callable[[], Any]], Any] | None = None) -> list[dict[str, Any]]:
    """One row per tree node for the inspector's Tree tab (§DASHBOARD-UX
    §5.1). ``gate_results`` (the gate pip strip) is read from the cached
    dispatch-time evaluation in ``audit/<node>.json`` via ``cached_read``
    when provided (the snapshot hot path), else live from disk — same
    "gates evaluated once, at dispatch" rule as ``node_detail``."""
    read = cached_read or (lambda path, loader: loader())
    rows: list[dict[str, Any]] = []
    for node in tree.nodes.values():
        audit_file = run_dir / "audit" / f"{node.id}.json"
        gate_results = read(audit_file, lambda p=audit_file: _read_gate_cache_any(p))
        artifact_file = node_artifact_path(run_dir, node.id)
        # §PERF: both artifact_tokens and artifact_count used to each read
        # this same file from disk independently, and artifact_count's read
        # (via the old `_artifact_count` -> `_has_content` -> `_read_text`)
        # bypassed `cached_read` entirely -- a full, uncached re-read of
        # every node's artifact on every single snapshot() call (every SSE
        # tick, ~every 1.5s). One cached read now backs both fields.
        artifact_text = read(artifact_file, lambda p=artifact_file: _read_text(p) or "")
        artifact_tokens = estimate_tokens(artifact_text)
        # §PERF: the versions directory listing (`_list_versions`, an
        # `iterdir()` + sort) was likewise uncached and done fresh per node
        # per snapshot. Cached the same way, keyed on the directory's own
        # (size, mtime_ns) stamp -- which changes when a version file is
        # added, same invalidation contract `cached_read` already documents.
        versions_directory = versions_dir(run_dir, node.id)
        version_names = read(versions_directory, lambda p=run_dir, nid=node.id: _list_versions(p, nid))
        artifact_count = (1 if artifact_text.strip() else 0) + len(version_names)
        rows.append(
            {
                "id": node.id,
                "brief": node.brief,
                "status": node.status,
                "shape": node.shape,
                "artifact": node.artifact,
                "judgment": len(node.judgment),
                "gates": len(node.gates),
                "gate_results": gate_results,
                "attempts": node.attempts,
                "depends_on": node.depends_on,
                "artifact_count": artifact_count,
                "artifact_tokens": artifact_tokens,
                "parent": node.parent,
            }
        )
    return rows
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


def _summarize_subagent(
    run_dir: Path,
    node_id: str,
    events: list[dict[str, Any]],
    logdir_for: Callable[[Path], Path | None] | None = None,
) -> dict[str, Any]:
    # ``_last_logdir`` is defined below this function, so it can't be a
    # default-arg here (def-time NameError); resolve at call time instead.
    if logdir_for is None:
        logdir_for = _last_logdir
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
    logdir = logdir_for(node_trace_path(run_dir, node_id))
    live = bool(logdir) and not completed

    # §F4: derived_status (idle|starting|thinking|tool|reviewing|done|failed)
    if completed:
        derived_status = "done" if status == "done" else "failed"
    elif attempts > 0 or live or status == "running":
        trace_text = _read_text(node_trace_path(run_dir, node_id))
        if not trace_text or not trace_text.strip():
            derived_status = "starting"
        else:
            last_role = ""
            for line in reversed(trace_text.splitlines()):
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                    role_val = str(rec.get("role") or rec.get("type") or "")
                    if role_val:
                        last_role = role_val
                        break
                except json.JSONDecodeError:
                    continue
            if last_role in ("thinking", "think"):
                derived_status = "thinking"
            elif last_role in ("tool", "system", "shell"):
                derived_status = "tool"
            elif last_role in ("reviewer", "review"):
                derived_status = "reviewing"
            else:
                derived_status = "thinking"
    else:
        derived_status = "idle"

    return {
        "id": node_id,
        "kind": _kind_of(node_id),
        "role": role,
        "status": status,
        "derived_status": derived_status,
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


def _read_gate_cache_any(path: Path) -> list[dict[str, Any]]:
    """§DASHBOARD-UX §5.1: the tree's gate-pip strip comes from the
    dispatch-time gate cache (``audit/<node>.json``) when a dispatch has
    written one; ``None`` (never dispatched) renders as all-dim pips
    client-side, so an undispatched node and a node whose gates all failed
    stay visually distinct."""
    from ..v1.gates import read_gate_cache

    cached = read_gate_cache(path)
    return cached if cached is not None else []


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
