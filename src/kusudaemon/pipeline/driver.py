"""The pipeline driver: intake -> survey -> plan -> pilot -> contract
freeze -> research -> execute -> assemble (PLAN.md §4, §10, §11).

Everything the web UI and the ``pipeline`` CLI commands run funnels
through this one class. Design rules, in PLAN.md order:

- **Durable progress, resumable phases (§10).** ``phase.json`` records
  ``{phase, status, detail}``, and each phase's *skip* decision is read
  from an artifact file, not from that marker: ``spec.md`` (intake),
  ``spine.json`` (survey), ``tree.json`` (plan), ``contract.md`` (pilot +
  freeze). research/execute/assemble are internally idempotent by their
  own layers (v4's finding cache, v1's round loop, v3's assembly loop), so
  re-running the driver on an existing run dir converges exactly where
  durable state left off — §10's property, composed rather than re-coded.
- **Human gates are disk approvals (§10, §11).** Intake answers and pilot
  edits go through ``approvals.jsonl`` (``approvals.py``): the driver
  creates a pending record and polls until any surface—web app or CLI—
  resolves it. Resume reuses an unanswered pending record instead of
  stacking a duplicate question.
- **Never interrupt mid-turn (§10).** ``halt.flag`` is checked at phase
  boundaries only; everything the v0-v4 layers already made crash-safe
  stays in their hands.
- **Per-node prompts assembled up front (§8).** ``build_node_prompt``
  renders brief + contract + inputs *before* a writer episode opens.

The driver never constructs an adapter: callers inject
``writer_adapter_factory`` / ``research_adapter_factory``; the defaults
come from ``backends.py`` driven by ``RunOptions.backend``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import urllib.error
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..adapters.base import AgentAdapter
from ..environment.base import Environment
from ..types import EpisodeBudget
from ..v6.direct import (
    DIRECT_MAX_ATTEMPTS,
    SINGLE_NODE_ID,
    build_single_node_tree,
    is_size_defect,
    run_direct_episode,
)
from ..v6.tiering import (
    ScopeEstimate,
    Tier,
    classify,
    escalate,
    estimate_scope_full,
    max_explorers_for,
    measure_signals,
    phases_for,
    tier_max,
)
from ..v6.work_object import WorkObject, survey_workspace, work_object_from_text, work_object_none
from ..v7.split import handle_split_proposal, maybe_derive_split_parent
from ..v0.events import EventLog
from ..v0.run_dir import write_text_atomic
from ..v0.run_dir import create_run_dir, ensure_node_trace_path, node_trace_path, events_path, glossary_path, manifest_path, node_artifact_path, spec_path
from ..v1.provider import OpenAICompatibleProvider, ProviderHTTPError
from ..v1.reviewer import ReviewVerdict
from ..v1.round_loop import run_round_loop
from ..v1.run_dir import ensure_audit_dir
from ..v1.tree import TaskNode, TaskTree
from ..v2.contract import ContractRule, freeze_contract, render_spec_rubric_to_contract
from ..v2.intake import (
    GlobalRubric,
    IntakeObjection,
    IntakeQuestion,
    QuestionSet,
    render_spec_md,
    run_intake,
)
from ..v2.pilot import (
    approve_pilot,
    run_pilot,
    select_pilot_nodes,
    snapshot_pilot_original,
)
from ..v2.planner import build_tree
from ..v2.retrieval import build_chunk_index
from ..v2.run_dir import contract_path
from ..v6.templates import merge_template_into_tree, write_tree_glossary
from ..v2.survey import (
    SpineUnit,
    assemble_spine,
    chunk_text,
    load_spine,
    materialize_units,
    save_spine,
    survey_chunks,
    unit_input_path,
)
from ..v3.assembly_loop import run_assembly_loop
from ..v3.document_review import (
    DocumentReviewResult,
    build_document_index,
    run_document_review,
    serialize_triage,
)
from ..v3.revalidate import Triage, apply_revalidation_triage, run_revalidation_pass, summarize_triage
from ..v4.probe_planner import plan_probes
from ..v4.research import ResearchQuery, run_research_query
from ..v4.research_loop import run_research_loop
from ..v4.run_dir import research_finding_path
from . import approvals as approval_store
from .backends import parse_research_plan
from .liveness import record_driver_start, record_heartbeat, start_heartbeat_thread
from .prompts import build_node_prompt
from .run_dir import halt_path, phase_path, run_spec_path, source_path, tier_path, tree_path

# Legacy static phase list (pre-§B2). Kept only as a backward-compatible
# re-export (``pipeline/__init__.py`` still exports it, and
# ``dashboard/rendering.py``/``static/app.js`` carry their own independent
# copies of the same seven names for progress-bar rendering — untouched by
# this workstream, §C4's job). The driver itself no longer iterates this:
# ``run()`` below computes its phase list per round from
# ``v6.tiering.phases_for(tier)`` instead (PLAN.md §A2 invariant 8/§B2).
PHASES = ("intake", "survey", "plan", "pilot", "research", "execute", "assemble")

# PLAN.md §B2: phase *names* are reused across tiers ("execute" means "one
# direct episode" for T0, "the round loop over a real tree.json" for
# T1-3), so a phase already run under one tier must not be mistaken for
# "done" once the tier has risen and the same name means something
# different against fresh state. These are exactly the phases whose
# ``_phase_done`` is "idempotent, always re-run" (never a durable-artifact
# check) — see ``run()``'s ``_ran_key``.
_TIER_SCOPED_PHASES = {"execute", "review", "research", "assemble", "verify"}

_HALTED = "halted"
_IN_PROGRESS = "in_progress"

# PLAN-zeromem.md §5.2c': per-node episode durations scale with the node's
# token budget instead of every node getting the same flat 30 minutes.
# NodeBudget.tokens defaults to 24_000 and v2/planner.py sets it per leaf
# (default via DEFAULT_TOKEN_BUDGET); those 24k tokens map to the harness's
# historical flat EpisodeBudget default of 1800s. Floors and ceilings keep
# a 400-token stub from getting a 30-second box and a generous chapter from
# an unbounded one — the point is bounding pathology, not tight packing
# (v1/gates.py's estimate_tokens feeds it: words/0.75, no tokenizer).
_REFERENCE_BUDGET_TOKENS = 24_000
_REFERENCE_DURATION_SECONDS = 1_800
_MIN_EPISODE_SECONDS = 300  # 5 minutes: a slow-but-correct node must not
# be converted into a hard max_attempts burn by a too-tight cap.
_MAX_EPISODE_SECONDS = 7_200  # 2 hours: past this the budget is pathological


def _budget_seconds(node: TaskNode) -> int:
    """Wall-clock ceiling for one node's episode, proportional to
    ``node.budget.tokens`` with a floor and a ceiling (PLAN-zeromem.md §5.2c').

    ``NodeBudget.calls`` stays deliberately unwired: gptme has no lever that
    would enforce a tool-call limit, so inventing one here would be fake
    enforcement — the docs (and ``v2/planner.py``'s leaf_gate) already note
    that calls is a plan-time estimate only.
    """
    tokens = node.budget.tokens or _REFERENCE_BUDGET_TOKENS
    seconds = round(tokens / _REFERENCE_BUDGET_TOKENS * _REFERENCE_DURATION_SECONDS)
    return max(_MIN_EPISODE_SECONDS, min(_MAX_EPISODE_SECONDS, seconds))


@dataclass
class RunOptions:
    """Everything the pipeline needs to (re)build itself from a run dir.

    Persisted to run.spec.json at first dispatch so a detached web app or
    ``pipeline resume`` can rebuild the environment from disk alone (§11:
    the web view "can be attached from anywhere")."""

    goal: str = ""
    backend: str = "gptme"
    model: str | None = None
    source_text: str = ""
    # PLAN.md §A3/§B1: the work object a run's Writers dispatch against.
    # Like source_text, a constructor input only — never round-tripped
    # through to_spec/from_spec (see the comment on "source" below): a
    # kind="workspace" WorkObject's `root` is a live filesystem reference,
    # not something to freeze into JSON across a resume. A caller resuming
    # a workspace-mode run re-supplies --workspace, which reconstructs this
    # fresh via v6.work_object.measure_workspace (pipeline/run.py) — the
    # same "disk is authoritative, argv just re-points at it" shape
    # source_text already has via source.txt.
    work_object: WorkObject | None = None
    compile_command: str | None = None
    research_plan: dict[str, list[ResearchQuery]] = field(default_factory=dict)
    max_rounds: int = 100
    max_attempts: int = 3
    dispatch_policy: str = "model"
    # PLAN.md §C2: parallel dispatch within each round-loop wave. 1 is
    # today's exact serial behavior; >1 runs that many Writer episodes
    # concurrently per round (wave fill is code-derived from the ready
    # set — an orchestrator config, not extra model calls). Only operators
    # who know their provider/workspace can tolerate it should raise it.
    max_parallel: int = 1
    document_review: bool = False
    # A2-1 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): deterministic
    # (embedding-based) survey is the default — model-mode surveying issued
    # ⌈n_chunks/8⌉ calls and was the dominant cost on large corpora.
    # _phase_survey already logs `survey_fallback` and degrades to the model
    # path when the retrieval extra isn't installed.
    survey_mode: str = "embedding"
    # A6-4 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): inline spans are on by
    # default — a writer episode that must *discover* its inputs with `read`
    # calls pays a full extra round trip per input with the whole
    # conversation resent each time; inlining the retrieved spans (BM25 +
    # dense, v2/retrieval.py, degrading gracefully to BM25-only or a plain
    # path list when the index is missing) lets most episodes write in one
    # turn. Bigger single prompt, strictly fewer turns.
    inline_spans: bool = True
    # PLAN.md §A4.4/§B2: a floor, never a ceiling (invariant 9) — the
    # classifier still runs and still measures a real tier; the tier
    # actually used for phases_for() is max(measured, tier_override).
    # One of "T0"/"T1"/"T2"/"T3", or None for "no forced floor".
    tier_override: str | None = None
    # A5-1 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): when set, intake never
    # runs — no questions, no objection surfacing; spec.md gets the
    # minimal rendering at the intake phase. Also lets the classify phase
    # skip its estimate call outright when the free size signals already
    # force ≥T2 (work_tokens >= 150_000): with intake disabled the call's
    # only remaining value (T0/T1 gating, intake triggering) is moot.
    no_intake: bool = False
    # PLAN.md §C3: when true and no explicit research_plan was supplied,
    # _phase_research builds one from the probe planner (windowed
    # complete_json over candidate leaves). The operator-supplied
    # research_plan still wins when present — explicit targeting overrides
    # auto-targeting the same way --tier overrides the classifier.
    auto_probe_plan: bool = True
    workspace_root: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.research_plan, str):
            from .backends import parse_research_plan
            self.research_plan = parse_research_plan(self.research_plan)
        if not self.workspace_root and self.work_object is not None and self.work_object.root is not None:
            self.workspace_root = str(self.work_object.root.resolve())

    def to_spec(self) -> dict[str, Any]:
        ws_root = self.workspace_root or (
            str(self.work_object.root.resolve())
            if self.work_object is not None and self.work_object.root is not None
            else None
        )
        spec = {
            "goal": self.goal,
            "backend": self.backend,
            "model": self.model,
            "compile_command": self.compile_command,
            "research_plan": [
                {"node_id": node_id, **asdict(query)}
                for node_id, queries in self.research_plan.items()
                for query in queries
            ],
            "max_rounds": self.max_rounds,
            "max_attempts": self.max_attempts,
            "dispatch_policy": self.dispatch_policy,
            "max_parallel": self.max_parallel,
            "document_review": self.document_review,
            "survey_mode": self.survey_mode,
            "inline_spans": self.inline_spans,
            "tier_override": self.tier_override,
            "no_intake": self.no_intake,
            "auto_probe_plan": self.auto_probe_plan,
        }
        if ws_root:
            spec["workspace_root"] = ws_root
        # §11.10.7: the corpus lives once, in source.txt. Embedding
        # source_text here duplicated the run's largest file into a JSON
        # every resume re-parses (*and* into the --detach argv, see
        # cli.py); the spec records where the corpus is instead. Legacy
        # specs that do embed it still load (from_spec reads the field).
        spec["source"] = "source.txt"
        return spec

    @staticmethod
    def from_spec(data: dict[str, Any]) -> "RunOptions":
        workspace_root = data.get("workspace_root")
        work_object = None
        if workspace_root:
            try:
                from ..v6.work_object import measure_workspace

                work_object = measure_workspace(workspace_root)
            except Exception:
                work_object = None
        return RunOptions(
            goal=str(data.get("goal", "")),
            backend=str(data.get("backend", "gptme")),
            model=data.get("model"),
            # Legacy specs embedded source_text; spec["source"] just names
            # the file in the run dir, which resume never needs to re-read —
            # source.txt already exists (driver only writes it when missing).
            source_text=str(data.get("source_text", "")),
            work_object=work_object,
            workspace_root=workspace_root,
            compile_command=data.get("compile_command"),
            research_plan=parse_research_plan(data.get("research_plan")),
            max_rounds=int(data.get("max_rounds", 100)),
            max_attempts=int(data.get("max_attempts", 3)),
            dispatch_policy=str(data.get("dispatch_policy", "model")),
            max_parallel=int(data.get("max_parallel", 1)),
            document_review=bool(data.get("document_review", False)),
            survey_mode=str(data.get("survey_mode", "embedding")),
            inline_spans=bool(data.get("inline_spans", True)),
            tier_override=data.get("tier_override"),
            no_intake=bool(data.get("no_intake", False)),
            auto_probe_plan=bool(data.get("auto_probe_plan", True)),
        )


@dataclass
class RunReport:
    status: str  # "done" | "halted" | "escalated" | "error"
    phase: str
    detail: str = ""
    tree_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


WriterAdapterFactory = Callable[[TaskNode], AgentAdapter]
ResearchAdapterFactory = Callable[[TaskNode, ResearchQuery], AgentAdapter]
ProbeAdapterFactory = Callable[[ResearchQuery], AgentAdapter]

# PLAN.md §A6/§B4: the pseudo-node id structural-exploration probes dispatch
# under. Deliberately not "explore-01" — that's already _phase_survey's own
# large-corpus reasoning-explainer pseudo-agent id (see that method below);
# using the same id would collide both dispatch ids (research_node_id's
# "<id>~research~<slug>" derivation) and, worse, the two pseudo-agents'
# trace.jsonl files. "explore" (no "-01" suffix) can't collide with either a
# real Writer node id (planner-built ids are always dot-hierarchical or a
# leaf id from a partition call, never bare "explore") or explore-01.
_EXPLORE_PROBE_NODE_ID = "explore"


def is_rate_limit_or_busy_error(exc: Exception | str) -> bool:
    msg = str(exc).lower()
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status in (429, 501):
        return True
    indicators = (
        "429",
        "too many requests",
        "rate limit",
        "rate_limit",
        "501",
        "too busy",
        "server busy",
        "overloaded",
        "capacity",
    )
    return any(ind in msg for ind in indicators)


# PLAN-AUDIT.md §E10: `_run_phase`'s retry policy used to be backwards — a
# 429/501 (already handled by its own patient ladder inside
# ``v1/provider.py``'s ``_call``, §D11) failed this level immediately, while
# a deterministic error (``ValueError``, a schema-validation
# ``ProviderError``, a ``KeyError``) got retried 3x, one second apart,
# **re-executing the whole phase body each time** — a ``classify`` phase
# that raises deterministically burned 3 provider calls instead of 1 before
# ever reporting the error. Fixed: only genuinely transient error classes
# (a 5xx that escaped provider.py's own short retry ladder, or a raw
# connection-level failure that never made it through ``_http_transport``'s
# wrapping) are retried here, capped at 2 attempts total with exponential
# backoff and jitter; everything else — including rate-limit/busy errors,
# which stay exactly as they were: reported immediately, never
# double-retried on top of the provider layer's own ladder — is reported on
# the first occurrence.
_PHASE_TRANSIENT_MAX_ATTEMPTS = 2
_PHASE_TRANSIENT_BASE_DELAY = 1.0
_PHASE_TRANSIENT_MAX_DELAY = 30.0


def is_transient_phase_error(exc: Exception) -> bool:
    """5xx ``ProviderHTTPError`` (provider.py already exhausted its own
    short exponential retry for these before letting one escape), or a raw
    ``URLError``/``TimeoutError`` that reached this level directly (e.g. a
    call site or test transport that doesn't route through
    ``_http_transport``'s wrapping into ``ProviderError``). A 4xx
    ``ProviderHTTPError`` other than 429/501 (handled separately by
    ``is_rate_limit_or_busy_error``, checked first at the call site so the
    two never double-classify the same exception) is a caller/schema bug,
    not a transient condition, and must not be retried."""
    if isinstance(exc, ProviderHTTPError):
        return 500 <= exc.status < 600
    return isinstance(exc, (urllib.error.URLError, TimeoutError))


class RecursiveDriver:
    """One pipeline run. Construction never touches the network; only
    :meth:`run` does, and each phase is individually resumable."""

    def __init__(
        self,
        run_dir: str | Path,
        *,
        provider: OpenAICompatibleProvider,
        options: RunOptions,
        env: Environment | None = None,
        writer_adapter_factory: WriterAdapterFactory | None = None,
        research_adapter_factory: ResearchAdapterFactory | None = None,
        probe_adapter_factory: ProbeAdapterFactory | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        # Resolved, not just wrapped: workspace_path/prompt_dir (below) are
        # both derived from this and fed into a shell command shaped
        # `cd {workspace_path} && ... < {prompt_path}` (cli_agent.py). If
        # run_dir stays relative (a caller's relative --runs-root, e.g.
        # "./.kusudaemon/runs"), prompt_path -- which already has run_dir's
        # own prefix baked in via prompt_dir -- gets re-resolved relative to
        # the *new* cwd after `cd`, doubling that prefix and pointing at a
        # path that was never created there: "/bin/sh: .../tmp/prompts/
        # episode_trace_*.md: No such file or directory" on every single
        # Writer dispatch, which is also why every episode looked like it
        # never started (no trace.jsonl line ever gets written).
        self.run_dir = Path(run_dir).resolve()
        self.provider = provider
        self.options = options
        self.env = env or _local_env(self.run_dir)
        self.writer_adapter_factory = writer_adapter_factory or self._default_writer_factory()
        self.research_adapter_factory = research_adapter_factory or self._default_research_factory()
        self.probe_adapter_factory = probe_adapter_factory or self._default_probe_adapter_factory()
        self.poll_interval = poll_interval
        create_run_dir(self.run_dir.parent, self.run_dir.name)
        self._write_source_and_spec()
        self.log = EventLog(events_path(self.run_dir))
        if self.provider is not None:
            if getattr(self.provider, "_should_abort", None) is None:
                self.provider._should_abort = self._halted
            if getattr(self.provider, "_on_model_fallback", None) is None:
                def _on_fallback(from_model: str, to_model: str, reason: str) -> None:
                    self._log({
                        "node_id": "-",
                        "role": "harness",
                        "round": 0,
                        "type": "model_fell_back",
                        "from": from_model,
                        "to": to_model,
                        "reason": reason,
                    })
                self.provider._on_model_fallback = _on_fallback
        # §D0c: record who is (supposed to be) making progress, so a
        # phase.json frozen on "in_progress" by a dead process can be told
        # apart from one genuinely mid-call.
        record_driver_start(self.run_dir)

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------
    async def run(self) -> RunReport:
        """PLAN.md §A2 invariants 8/9, §B2: ``classify`` always runs first
        (phase index 0, through the same ``_run_phase`` machinery as every
        other phase — it just happens to be the one that decides the rest).
        Once it's done, the remaining phase list is recomputed from
        ``tier.json`` on **every** iteration of the loop below, not just
        once — a phase that bumps the tier mid-run (an escalation trigger
        firing inside ``_phase_execute``/``_phase_assemble``) makes the very
        next iteration see the grown list and pick up whatever it now
        requires that hasn't run yet, in the table's order. Because tiers
        only ever rise (invariant 9) and nothing already-done is discarded,
        this is safe: a phase already completed under the old tier is never
        revisited by name alone — see ``_ran_key`` for why a name like
        "execute" must be tracked per-tier rather than once ever (it means
        something structurally different at T0 than at T2)."""
        report = await self._run_phase("classify", round_index=0)
        # B2-3: a heartbeat thread keeps driver.pid.json's heartbeat_ts
        # fresh while this driver thread is alive — the signal that lets
        # liveness tell a working driver from a dead one even when the
        # driver is a thread inside the long-lived serve process. Stopped
        # in the finally below, so a completed run's heartbeat goes stale
        # and liveness stops considering it active (phase.json is terminal
        # by then anyway).
        heartbeat = start_heartbeat_thread(self.run_dir)
        try:
            report = await self._run_phase("classify", round_index=0)
            ran: set[str] = {"classify"}
            round_index = 1
            if report.status == "done":
                while True:
                    tier = self._current_tier()
                    next_phase: str | None = None
                    next_key: str | None = None
                    for candidate in phases_for(tier):
                        key = self._ran_key(candidate, tier)
                        if key not in ran:
                            next_phase, next_key = candidate, key
                            break
                    if next_phase is None:
                        break
                    if self._halted():
                        self._set_phase(next_phase, _HALTED, detail=f"halted before {next_phase}")
                        self._log({"node_id": "-", "role": "harness", "round": round_index, "type": "halting"})
                        report = RunReport(status="halted", phase=next_phase, detail="halted by operator")
                        break
                    report = await self._run_phase(next_phase, round_index=round_index)
                    if report.status != "done":
                        break
                    round_index += 1
                    ran.add(next_key)
            if report.status == "done" and next_phase is None:
                report = RunReport(status="done", phase=phases_for(self._current_tier())[-1])
            report.tree_counts = _count_statuses(self._load_tree())
            # §11.9: run_completed is a claim about the run's outcome — logging
            # it after a halt/escalate/error made the event log disagree with
            # the report.
            if report.status == "done":
                self._log({"node_id": "-", "role": "harness", "round": 0, "type": "run_completed"})
            return report
        finally:
            heartbeat.stop()

    @staticmethod
    def _ran_key(phase: str, tier: str) -> str:
        """§B2: most phase names are "run once, ever, for this run" —
        their own ``_phase_done`` already guards that (spec.md/spine.json/
        tree.json/contract.md existing is tier-independent: a tree.json
        built by T2's planner is still a valid tree.json once escalated to
        T3, no need to rebuild it). The handful in ``_TIER_SCOPED_PHASES``
        mean something different depending on which tier ran them (T0's
        "execute" is one direct episode with no tree.json at all; T2's is a
        multi-node round loop) and are always safe to re-run (idempotent by
        construction), so they're tracked per-tier instead of once."""
        return f"{phase}@{tier}" if phase in _TIER_SCOPED_PHASES else phase

    async def _run_phase(self, phase: str, *, round_index: int) -> RunReport:
        if self._phase_done(phase):
            return RunReport(status="done", phase=phase)
        self._set_phase(phase, _IN_PROGRESS)
        self._log({"node_id": "-", "role": "harness", "round": round_index, "type": "phase_started", "phase": phase})
        
        attempt = 0
        while True:
            attempt += 1
            if self._halted():
                self._set_phase(phase, _HALTED, detail=f"halted in {phase}")
                return RunReport(status="halted", phase=phase, detail="halted by operator")
            try:
                outcome: Any = await getattr(self, f"_phase_{phase}")()
                break
            except Exception as exc:  # noqa: BLE001 — the phase boundary is the reporter
                # §E10: rate-limit/busy errors are never retried at this
                # level — v1/provider.py's own ladder (§D11) already spent
                # up to five hours on them before one could even reach here,
                # so a second retry layer on top would only double that
                # wait. Checked first so it can never also match
                # ``is_transient_phase_error`` (a 429/501's own message can
                # overlap "server busy"-style wording).
                if is_rate_limit_or_busy_error(exc):
                    self._set_phase(phase, "error", detail=str(exc))
                    self._log(
                        {"node_id": "-", "role": "harness", "round": round_index, "type": "phase_failed", "phase": phase, "error": str(exc)}
                    )
                    return RunReport(status="error", phase=phase, detail=str(exc))
                if not is_transient_phase_error(exc) or attempt >= _PHASE_TRANSIENT_MAX_ATTEMPTS:
                    # Deterministic error (ValueError, schema-validation
                    # ProviderError, KeyError, ...) — reported on the first
                    # occurrence; retrying would just fail identically. A
                    # transient error that has already used up its retry
                    # budget falls through the same way.
                    self._set_phase(phase, "error", detail=str(exc))
                    self._log(
                        {"node_id": "-", "role": "harness", "round": round_index, "type": "phase_failed", "phase": phase, "error": str(exc)}
                    )
                    return RunReport(status="error", phase=phase, detail=str(exc))
                # Transient (5xx / URLError / TimeoutError) with retry
                # budget remaining — exponential backoff with jitter,
                # capped at _PHASE_TRANSIENT_MAX_ATTEMPTS total attempts.
                err_msg = str(exc)
                self._set_phase(
                    phase,
                    _IN_PROGRESS,
                    detail=f"Auto-resuming (attempt {attempt}/{_PHASE_TRANSIENT_MAX_ATTEMPTS}) after transient error: {err_msg}",
                )
                self._log(
                    {
                        "node_id": "-",
                        "role": "harness",
                        "round": round_index,
                        "type": "phase_auto_resuming",
                        "phase": phase,
                        "attempt": attempt,
                        "error": err_msg,
                    }
                )
                delay = min(
                    _PHASE_TRANSIENT_BASE_DELAY * (2 ** (attempt - 1)),
                    _PHASE_TRANSIENT_MAX_DELAY,
                )
                await asyncio.sleep(delay * random.uniform(0.8, 1.2))
        status = "done" if outcome is not False else "escalated"
        # Preserve a detail the phase body already wrote (e.g.
        # _phase_research's "skipped: ...") instead of clobbering it with a
        # blank tail call (PLAN-zeromem.md §11.4).
        existing = _read_phase(self.run_dir)
        detail = existing.get("detail", "") if existing.get("phase") == phase else ""
        self._set_phase(phase, status, detail=detail)
        self._log(
            {
                "node_id": "-",
                "role": "harness",
                "round": round_index,
                "type": "phase_done",
                "phase": phase,
                "status": status,
            }
        )
        return RunReport(status=status, phase=phase)

    def _reasoning_sink(self, pseudo_node_id: str) -> Callable[[str], None]:
        trace_path = node_trace_path(self.run_dir, pseudo_node_id)
        def _sink(text: str) -> None:
            if not text:
                return
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            with trace_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"role": "thinking", "text": text}) + "\n")
        return _sink

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------
    async def _phase_classify(self) -> None:
        """PLAN.md §A4/§B2: the one advisory model call that decides how
        much of the rest of the pipeline this run actually needs. Writes
        ``tier.json``; ``run()`` re-reads it every iteration of its phase
        loop, and every escalation trigger (below) rewrites just its
        ``tier`` field via ``_write_tier_record``.

        ``--tier`` is a floor, never a ceiling (invariant 9): the estimate
        call still runs (so the record is honest about what the harness
        would have chosen on its own) *unless* the floor is already T3 —
        T3 already runs every phase the estimate could have gated, so
        spending a call whose verdict is guaranteed to be overridden is
        pure waste. Any other forced floor (T0/T1/T2) still measures for
        real, because ``max(measured, floor)`` can still come out higher
        than the floor itself.

        A5-1 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md) adds a second skip: an
        explicitly intake-disabled run (``--no-intake``) whose free size
        signals already force ≥T2 (``work_tokens >= 150_000``) — the call
        would only gate T0/T1 and trigger intake, both moot. Measured tier
        then falls out of ``classify`` on the empty estimate, which
        resolves to T2.

        A5-2: the call itself is the merged ``estimate_scope_full`` —
        scope estimate plus intake round-1 questions/objections in one
        response. The round-1 set is stored in ``tier.json`` under
        ``intake_round1`` so a resume re-asks the exact same questions
        without re-calling the model.

        A5-5: ``needs_explore`` folds in the T1 gate — a T1 run explores
        only when the estimator could not bound the blast radius
        (``files_touched == "unknown"``), which ``classify`` forces to ≥T2
        anyway; the formula keeps the stored record honest for an
        operator-forced ``tier set T1``.
        """
        work = self._effective_work_object()
        goal = self.options.goal.strip()
        if not goal:
            raise ValueError("goal is required")
        signals = measure_signals(goal, work)
        override = (self.options.tier_override or "").upper() or None
        intake_disabled = self.options.no_intake
        if override == "T3" or (intake_disabled and signals.work_tokens >= 150_000):
            estimate = ScopeEstimate()
            question_set = QuestionSet()
            measured: Tier = "T3" if override == "T3" else classify(signals, estimate)
            if intake_disabled and signals.work_tokens >= 150_000 and override != "T3":
                self._log(
                    {
                        "node_id": "-",
                        "role": "harness",
                        "round": 0,
                        "type": "scope_estimate_skipped",
                        "reason": (
                            "signals.work_tokens >= 150_000 forces >= T2 and "
                            "--no-intake disables the intake trigger; the "
                            "estimate call would buy nothing"
                        ),
                    }
                )
        else:
            estimate, question_set = estimate_scope_full(
                goal,
                work,
                self.provider,
                on_reasoning=self._reasoning_sink("phase-classify"),
                streaming=True,
            )
            measured = classify(signals, estimate)
        tier: Tier = tier_max(measured, override) if override else measured
        intake_round1 = {
            "questions": [
                {
                    "id": question.id,
                    "text": question.text,
                    "default_assumption": question.default_assumption,
                }
                for question in question_set.questions
            ],
            "objections": [
                {"claim": objection.claim, "why": objection.why, "options": list(objection.options)}
                for objection in question_set.objections
            ],
        }
        payload = {
            "tier": tier,
            "measured_tier": measured,
            "override": override,
            "signals": asdict(signals),
            "estimate": asdict(estimate),
            "needs_intake": bool(
                question_set.questions or question_set.objections
            )
            and not intake_disabled,
            # A5-5: T1 explores only when the estimator couldn't bound the
            # blast radius (files_touched == "unknown"); classify() forces
            # >= T2 for "unknown", so this can only fire on an
            # operator-forced T1 (tier set command).
            "needs_explore": not estimate.answerable_without_exploration
            and (tier != "T1" or estimate.files_touched == "unknown"),
            "intake_round1": intake_round1 if question_set.questions or question_set.objections else None,
            "ts": time.time(),
        }
        write_text_atomic(
            tier_path(self.run_dir),
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        self._log(
            {
                "node_id": "-",
                "role": "harness",
                "round": 0,
                "type": "tier_classified",
                "tier": tier,
                "measured_tier": measured,
                "override": override,
            }
        )

    def _effective_work_object(self) -> WorkObject:
        """PLAN.md §A3: every run has *some* WorkObject by classify time,
        even one that never passed ``--workspace`` — today's default
        (``RunOptions.work_object is None``) falls back to the
        ``source_text``/``kind="none"`` construction §A3 describes as the
        "deprecated alias" path v6/work_object.py's own docstring
        promises."""
        if self.options.work_object is not None:
            return self.options.work_object
        text = self.options.source_text.strip()
        if text:
            return work_object_from_text(self.options.source_text)
        return work_object_none()

    def _read_tier_record(self) -> dict[str, Any]:
        try:
            payload = json.loads(tier_path(self.run_dir).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _current_tier(self) -> Tier:
        tier = self._read_tier_record().get("tier")
        if tier not in ("T0", "T1", "T2", "T3"):
            raise RuntimeError(
                "tier.json is missing or invalid — the classify phase must "
                "run before any tier-dependent phase can"
            )
        return tier  # type: ignore[return-value]

    def _write_tier_record(self, **updates: Any) -> None:
        """Read-modify-write over ``tier.json`` — every escalation trigger
        routes through this so ``signals``/``estimate``/``needs_intake``/
        ``needs_explore`` (all decided once, at classify time) survive a
        tier bump untouched; only the fields an escalation actually changes
        (``tier``, plus ``ts``) get overwritten."""
        record = self._read_tier_record()
        record.update(updates)
        record["ts"] = time.time()
        write_text_atomic(
            tier_path(self.run_dir),
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def _escalate_tier(self, trigger: str, *, node_id: str = "-", **extra: Any) -> Tier:
        current = self._current_tier()
        new_tier = escalate(current, trigger)
        self._write_tier_record(tier=new_tier)
        self._log(
            {
                "node_id": node_id,
                "role": "harness",
                "round": 0,
                "type": "run_tier_escalated",
                "trigger": trigger,
                "from": current,
                "to": new_tier,
                **extra,
            }
        )
        return new_tier

    async def _phase_intake(self) -> None:
        goal = self.options.goal.strip()
        if not goal:
            raise ValueError("goal is required")
        if self.options.no_intake:
            # A5-1: --no-intake — the operator waived clarification and
            # objection surfacing up front. spec.md still has to exist with
            # a "## Goal" section (both for _phase_done("intake") and
            # because build_node_prompt's _goal_and_rubric_block reads it),
            # so it's written directly — zero provider calls either way.
            self._log(
                {
                    "node_id": "-",
                    "role": "harness",
                    "round": 0,
                    "type": "phase_skipped",
                    "phase": "intake",
                    "reason": "--no-intake: operator waived clarification",
                }
            )
            self._write_minimal_spec(goal)
            return
        needs_intake = bool(self._read_tier_record().get("needs_intake", True))
        if not needs_intake:
            # PLAN.md §A4.3: "intake? fires only when ambiguities or
            # objections is non-empty" — the classify call already
            # established there were none, so re-running the full
            # CLAUDE.md §4.1 interview here would be spending calls to ask
            # questions the estimator already said didn't need asking.
            # spec.md still has to exist with a "## Goal" section (both for
            # _phase_done("intake") and because build_node_prompt's
            # _goal_and_rubric_block reads it), so it's written directly —
            # zero provider calls either way.
            self._log(
                {
                    "node_id": "-",
                    "role": "harness",
                    "round": 0,
                    "type": "phase_skipped",
                    "phase": "intake",
                    "reason": "estimate reported no ambiguities or objections",
                }
            )
            self._write_minimal_spec(goal)
            return
        record = self._read_tier_record()
        estimate = record.get("estimate") or {}
        ambiguities = [str(item) for item in (estimate.get("ambiguities") or [])]
        objections = [str(item) for item in (estimate.get("objections") or [])]
        # A5-2: the classify call already produced round 1's question set
        # in the merged estimate call; tier.json carries it so a resume
        # re-asks the exact same questions without a fresh model call.
        initial_question_set: QuestionSet | None = None
        round1 = record.get("intake_round1") or {}
        questions = [item for item in (round1.get("questions") or []) if isinstance(item, dict)]
        objections_out = [
            item for item in (round1.get("objections") or []) if isinstance(item, dict)
        ]
        if questions or objections_out:
            initial_question_set = QuestionSet(
                questions=tuple(
                    IntakeQuestion(
                        id=str(item.get("id") or f"q{index + 1}"),
                        text=str(item.get("text", "")),
                        default_assumption=str(item.get("default_assumption", "")),
                    )
                    for index, item in enumerate(questions)
                ),
                objections=tuple(
                    IntakeObjection(
                        claim=str(item.get("claim", "")),
                        why=str(item.get("why", "")),
                        options=tuple(str(option) for option in (item.get("options") or [])),
                    )
                    for item in objections_out
                ),
            )
        run_intake(
            self.run_dir,
            goal,
            ambiguities,
            objections,
            self.provider,
            self._ask_intake_round,
            on_reasoning=self._reasoning_sink("phase-intake"),
            initial_question_set=initial_question_set,
            streaming=True,
        )

    def _write_minimal_spec(self, goal: str) -> None:
        rubric = GlobalRubric(
            goal=goal,
            answers={},
            assumptions=[
                "Intake was skipped: the PLAN.md §A4.2 scope estimate "
                "reported no ambiguities or objections, so no clarifying "
                "questions were asked (§B2)."
            ],
        )
        spec_path(self.run_dir).write_text(render_spec_md(rubric), encoding="utf-8")

    def _ask_intake_round(
        self,
        round_index: int,
        questions: tuple[IntakeQuestion, ...],
        objections: tuple[IntakeObjection, ...],
    ) -> dict[str, str]:
        """PLAN.md §A5.3/§B3: one approval carries every question in the
        round — a form, not one blocking prompt per question (the design
        this superseded, keyed one approval per rubric *dimension*, seven
        of them). Keyed on ``round`` (not on any single question's id) so a
        resume that restarts intake reuses this round's own pending record
        (§11.9's reasoning, carried over) rather than stacking a duplicate.
        Objections ride along in the message body — they are shown to the
        operator here even though only the questions themselves are
        individually answerable; see ``v2.intake.run_intake``'s docstring
        for why that's the whole mechanism rather than a per-objection
        acknowledge action."""
        lines: list[str] = []
        if objections:
            lines.append("Objections raised by scope estimation:")
            for objection in objections:
                detail = f" — {objection.why}" if objection.why else ""
                options = f" (options: {', '.join(objection.options)})" if objection.options else ""
                lines.append(f"- {objection.claim}{detail}{options}")
            lines.append("")
        if questions:
            lines.append("Questions (leave blank to accept the stated default assumption):")
            for question in questions:
                default = f" [default if unanswered: {question.default_assumption}]" if question.default_assumption else ""
                lines.append(f"- [{question.id}] {question.text}{default}")
        approval = self._ask(
            "intake_questions",
            title=f"Intake round {round_index}",
            message="\n".join(lines),
            context={
                "round": round_index,
                "objections": [
                    {"claim": o.claim, "why": o.why, "options": list(o.options)}
                    for o in objections
                ],
            },
            questions=[
                {
                    "id": question.id,
                    "text": question.text,
                    "default_assumption": question.default_assumption,
                }
                for question in questions
            ],
        )
        return dict(approval.answers)

    async def _phase_survey(self) -> None:
        from ..v2.embeddings import embeddings_available
        from ..v2.survey import prefold_chunks, survey_chunks_deterministic

        work = self.options.work_object
        if work is not None and work.kind == "workspace":
            # PLAN.md §A3 point 2 / §B2: a workspace is never copied into
            # the run dir and has no chunk-range structure to vote
            # boundaries over — survey_workspace (v6/work_object.py, §B1)
            # already does the model-free "gitignore-aware walk, group by
            # directory" SpineUnit construction; there is nothing here for
            # the model-driven boundary voting below to do. This is the
            # gap PLAN.md's own §B1 results log named explicitly:
            # "_phase_survey still requires source_text unconditionally —
            # that's §B2's job."
            units = survey_workspace(work)
            save_spine(self.run_dir, units)
            return

        source = source_path(self.run_dir).read_text(encoding="utf-8").strip()
        if not source:
            # PLAN.md §D4: this used to synthesize a single SpineUnit
            # labeled "The goal", which build_tree then turned into one
            # forced leaf briefed "Produce the artifact for The goal
            # (single unit, cannot split further)" — is_complete() came
            # back true and the run reported "done" having produced an
            # artifact about nothing. A corpus-less goal is a real,
            # first-class case (PLAN.md §A3's kind="none"), but until that
            # lands a run with no source must fail loudly rather than
            # silently converge on a fake success.
            raise ValueError(
                "no source text: a corpus-less run (--source omitted or "
                "empty) is not yet supported as a real work-object kind "
                "(PLAN.md §A3 kind=\"none\") — the harness previously faked "
                "success on a single meaningless node instead of failing "
                "here (§D4). Pass --source with the goal's corpus, or wait "
                "for kind=\"none\" support."
            )
        else:
            chunks = chunk_text(source)
            # Subagent is optional — only spawn subagent if file/task is large
            is_large_corpus = len(chunks) > 10 or len(source) > 50000
            subagent_id = "explore-01" if is_large_corpus else None

            if subagent_id:
                self._log({"node_id": subagent_id, "role": "explorer", "round": 0, "type": "session_captured", "phase": "survey"})
            try:
                # A2-4 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): pre-fold chunks
                # up to ~min-unit size before surveying. assemble_spine folds
                # sub-800-token units away afterward anyway, so surveying the
                # merged list loses no resolution while cutting the model-mode
                # call count ~5–8× and the embedding-mode vector count with
                # it. is_large_corpus is computed from the raw chunks above so
                # the explorer-subagent decision is unchanged.
                chunks = prefold_chunks(chunks)
                if self.options.survey_mode == "embedding" and embeddings_available():
                    votes = survey_chunks_deterministic(chunks)
                else:
                    if self.options.survey_mode == "embedding":
                        self._log(
                            {
                                "node_id": subagent_id or "-",
                                "role": "harness",
                                "round": 0,
                                "type": "survey_fallback",
                                "reason": (
                                    "embedding mode requested but "
                                    "kusudaemon[retrieval] is not installed; "
                                    "falling back to the model survey"
                                ),
                            }
                        )
                    # This wraps plain provider.complete_json calls, not a
                    # gptme episode -- deliberately not interactive (§3:
                    # only the Writer needs a tool loop). But the model may
                    # still return reasoning_content alongside its JSON vote
                    # (§12), and that was previously discarded entirely,
                    # leaving the dashboard's explore-01 card with nothing to
                    # show. Surface it (thinking only, no tool loop) the same
                    # way a real gptme trace does, so the Chat tab renders it
                    # without any dashboard-side changes.
                    on_reasoning = (
                        (lambda text: self._append_explorer_reasoning(subagent_id, text))
                        if subagent_id
                        else None
                    )
                    votes = survey_chunks(
                        chunks, self.provider, on_reasoning=on_reasoning, streaming=True
                    )
            finally:
                if subagent_id:
                    # "episode_completed", not the made-up "session_ended" this
                    # replaced: dashboard/state.py's _summarize_subagent only
                    # flips a subagent's status/completed off of that exact
                    # type string (v0's event vocabulary), so anything else
                    # left this pseudo-agent showing "running" forever.
                    self._log({"node_id": subagent_id, "role": "explorer", "round": 0, "type": "episode_completed", "status": "done"})
            units = assemble_spine(chunks, votes)
            materialize_units(self.run_dir, chunks, units)
            build_chunk_index(self.run_dir, chunks, units)
        save_spine(self.run_dir, units)

    async def _phase_explore(self) -> None:
        """PLAN.md §A4.3: "explore" is the v6 phase-vocabulary name over
        what §4.2/today's ``_phase_survey`` does — discovering a work
        object's structure into ``spine.json``, the same operation whether
        the work object is a corpus or a workspace (§B1's
        ``survey_workspace``). Kept as its own method rather than a rename
        of ``_phase_survey`` for two reasons: (1) pre-existing tests call
        ``_phase_survey`` directly by name (§D4's corpus-less-raises test,
        the explorer-reasoning test) and stay correct unmodified; (2) a
        caller reading ``tier.json``'s phase list sees the name §A4.3's
        table actually uses.

        §A4.3's "explore? fires only when ``answerable_without_exploration``
        is false" gates two things now (PLAN.md §B4): producing
        ``spine.json`` is *not* itself optional — "plan" needs it
        unconditionally, at every tier that has a plan phase — so this
        method always ensures it exists regardless of ``needs_explore``.
        What ``needs_explore`` actually gates is (1) structural exploration
        — one bounded probe per top-level ``SpineUnit``, capped by
        ``max_explorers_for(tier)``, T2+ only (T1 has no "plan" phase to
        feed a summary into; T0 has no "explore" phase at all) — and (2)
        running v4's research loop early when the operator supplied an
        explicit ``research_plan`` (same gate ``_phase_research`` already
        uses for the "targeted, post-intake" pattern, §A6's second bullet,
        still carried unchanged from before this workstream).
        """
        tier = self._current_tier()
        if "plan" not in phases_for(tier):
            # PLAN.md §A4.3: T1 has no "plan" phase and builds its single
            # node from the goal in code (build_single_node_tree) — it
            # never reads spine.json, so ensuring one exists here (which,
            # for a corpus-less/workspace-less run, means _phase_survey
            # raising per §D4) turned "small task, no corpus, no
            # workspace" into a hard failure at explore for a tier that
            # never needed a spine in the first place. T0 never reaches
            # this method at all (no "explore" phase), so only T1 hits
            # this branch in practice.
            self._log(
                {
                    "node_id": "-",
                    "role": "harness",
                    "round": 0,
                    "type": "phase_skipped",
                    "phase": "explore_spine",
                    "reason": "tier has no plan phase",
                }
            )
            return
        if not (self.run_dir / "spine.json").exists():
            await self._phase_survey()
        needs_explore = bool(self._read_tier_record().get("needs_explore", True))
        if not needs_explore:
            self._log(
                {
                    "node_id": "-",
                    "role": "harness",
                    "round": 0,
                    "type": "phase_skipped",
                    "phase": "explore_research",
                    "reason": "estimate reported answerable_without_exploration",
                }
            )
            return
        if tier in ("T2", "T3"):
            await self._run_structural_exploration(tier)
        if self.options.research_plan:
            # PLAN-AUDIT.md §E14: this delegates into _phase_research as a
            # sub-step of the still-running "explore" phase, never as its
            # own top-level phase dispatch — pass "explore" as the phase
            # name to stamp so a capability-refusal skip inside
            # _phase_research (below) writes phase.json's "phase" field as
            # "explore", not a hardcoded "research" that would momentarily
            # disagree with what _run_phase itself started and is about to
            # tail-write once this method returns.
            await self._phase_research(phase="explore")

    async def _run_structural_exploration(self, tier: Tier) -> None:
        """PLAN.md §A6 first bullet/§B4: "one probe per top-level unit of
        the work object, up to ``max_explorers`` (tier cap)". Pre-plan,
        T2+ only (the caller enforces that). Applies identically whether
        the work object is a ``kind="workspace"`` repo or a ``kind="text"``
        corpus — both already have real per-unit content on disk by this
        point (``survey_workspace``'s members, or corpus mode's
        materialized ``spine/<unit>.md`` — §A6's table lists both
        ``workspace`` and ``corpus`` probe kinds explicitly for exactly this
        reason). Three independent code-side cost fences, all real, none
        reinvented here (§A6's own words): a per-probe ``EpisodeBudget``
        (default, same as every other v4 research dispatch); this method's
        own ``cap`` slice, so a work object with 4 top-level units and one
        with 400 both dispatch at most ``max_explorers_for(tier)`` probes;
        and ``run_research_query``'s existing 300-token finding cap.

        **Which units get probed when there are more than the cap**: the
        largest ``cap`` units by token count (ties broken by id for
        determinism) — the ones a planner partitioning by size would need
        the most help with, and the choice that makes the cap's cost bound
        exact regardless of how the caller orders ``spine.json``.

        Findings land at ``research_finding_path(run_dir, "explore",
        unit.id)`` — reusing v4's existing finding-path scheme (rather than
        the literal ``scratch/explore/<unit>.md`` §A6's table shows) so
        idempotency (a probe with an existing nonempty finding is not
        re-dispatched) comes for free from ``run_research_query``'s own
        cache check instead of being reimplemented here.
        """
        units = load_spine(self.run_dir)
        if not units:
            return
        cap = max_explorers_for(tier)
        if cap <= 0:
            return
        selected = sorted(units, key=lambda unit: (-unit.tokens, unit.id))[:cap]
        work = self._effective_work_object()
        probe_kind = "workspace" if work.kind == "workspace" else "corpus"
        budget = EpisodeBudget()
        for unit in selected:
            query = ResearchQuery(
                slug=unit.id,
                kind=probe_kind,
                question=self._structural_probe_question(unit, work),
            )
            adapter = self.probe_adapter_factory(query)
            await run_research_query(
                self.run_dir, _EXPLORE_PROBE_NODE_ID, query, adapter, self.env, budget
            )

    def _structural_probe_question(self, unit: SpineUnit, work: WorkObject) -> str:
        """The one question every structural-exploration probe answers —
        generic on purpose (§A6: "a strictly better partition input than
        the label alone", not a targeted question about run-specific
        content). ``workspace`` probes get the unit's own member paths (so
        they know where to point their read-only list/grep tools without
        having to rediscover the grouping ``survey_workspace`` already
        computed); ``corpus`` probes get the materialized unit file's path
        directly, since a corpus probe's allowlist is `read` alone."""
        if work.kind == "workspace":
            members = ", ".join(unit.members[:20]) or "(no files)"
            return (
                f'This is the "{unit.label}" portion of a codebase a planner '
                f"is about to partition into work items. Its files (relative "
                f"to your workspace root) include: {members}. Using your "
                f"read-only list/grep/read tools, summarize in <=300 tokens: "
                f"what this part of the codebase does, how it's organized, "
                f"and anything a planner partitioning work here should know "
                f"(natural sub-boundaries, risky areas, cross-cutting "
                f"concerns)."
            )
        unit_path = unit_input_path(self.run_dir, unit)
        return (
            f'This is the "{unit.label}" unit of a text corpus a planner is '
            f"about to partition into work items. Read {unit_path} and "
            f"summarize in <=300 tokens: its structure, purpose, and "
            f"anything a planner partitioning work here should know."
        )

    async def _phase_plan(self) -> None:
        probe_sink: list[dict[str, Any]] = []
        tree = build_tree(
            load_spine(self.run_dir),
            self.provider,
            input_path_for=lambda unit: unit_input_path(self.run_dir, unit),
            log=self.log,
            unit_summary_for=self._explore_summary_for,
            on_reasoning=self._reasoning_sink("phase-plan"),
            probe_sink=probe_sink,
            streaming=True,
        )
        # A5-3: fold the plan call's own probe suggestions (≤2, top-level
        # only) into the research phase — written to disk now so a resume
        # re-uses them without re-calling the model; _build_auto_probe_plan
        # prefers them over the separate windowed plan_probes call.
        if probe_sink:
            probe_plan_path = self.run_dir / "probe_plan.json"
            write_text_atomic(
                probe_plan_path,
                json.dumps({"probes": probe_sink}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
            self._log(
                {
                    "node_id": "-",
                    "role": "harness",
                    "round": 0,
                    "type": "probe_plan_from_plan_call",
                    "detail": f"{len(probe_sink)} probe suggestions folded into the plan call",
                }
            )
        # PLAN.md §C1: the planner's per-leaf ``apply_template_to_node``
        # cannot know run_dir, so re-apply with the absolute glossary path
        # (idempotent union — a leaf that already carries the template's
        # gates/judgment keeps them; only `terms_defined` gets rewritten to
        # its absolute form). The glossary itself is written once, right
        # after the tree is saved, from the templates the tree resolved to.
        merge_template_into_tree(tree, glossary_path=glossary_path(self.run_dir))
        tree.save(tree_path(self.run_dir))
        if write_tree_glossary(self.run_dir, tree):
            self._log(
                {
                    "node_id": "-",
                    "role": "harness",
                    "round": 0,
                    "type": "glossary_written",
                    "detail": "template glossary written to glossary.json",
                }
            )
        # PLAN.md §A10: at T2 (and only T2 — T3 has a real ``pilot`` phase
        # that derives contract.md from edit-diffs; T0/T1 have no plan phase
        # at all, so this branch is unreachable there), write contract.md
        # from spec.md by script. Zero model calls, no human gate, no
        # ``awaiting_approval`` state ever entered — T2's whole reason for
        # existing is "the operator expected this to take four minutes," and
        # parking it on a pilot approval form would reintroduce exactly the
        # overnight-wait failure mode §A10 was written to forbid. The script
        # renderer (v2/contract.py) is the third and last allowed writer of
        # contract.md, after pilot derivation and explicit user amendment.
        tier = self._current_tier()
        if tier == "T2" and not contract_path(self.run_dir).exists():
            render_spec_rubric_to_contract(self.run_dir)
            self._log(
                {
                    "node_id": "-",
                    "role": "harness",
                    "round": 0,
                    "type": "contract_rendered_from_spec",
                    "tier": tier,
                }
            )

    def _explore_summary_for(self, unit: SpineUnit) -> str:
        """PLAN.md §A7 point 3/§B4: reads back whatever
        ``_run_structural_exploration`` wrote for this unit during
        "explore" — empty when nothing did (T1's escalation path never runs
        explore's structural-exploration branch; a unit outside the top
        ``max_explorers_for(tier)`` picks never got probed either).
        ``plan_level``/``build_tree`` treat an empty string as "no summary
        line," so this degrades to today's label-only rendering rather than
        failing."""
        path = research_finding_path(self.run_dir, _EXPLORE_PROBE_NODE_ID, unit.id)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()

    async def _phase_pilot(self) -> None:
        tree = self._load_tree()
        rules: list[ContractRule] = []
        selected = select_pilot_nodes(tree)
        for node in sorted(selected.values(), key=lambda n: n.id):
            previous = self.log.last_event(node.id, "pilot_approved")
            if previous is not None:
                rules.extend(
                    ContractRule(source=node.id, shape=node.shape, text=text)
                    for text in previous.get("rules", [])
                    if isinstance(text, str) and text
                )
                continue
            artifact = _read_artifact(self.run_dir, node.id)
            if not artifact:
                await run_pilot(
                    self.run_dir,
                    node,
                    self._prompt_for_node(node),
                    self.writer_adapter_factory(node),
                    self.env,
                    EpisodeBudget(max_duration_seconds=_budget_seconds(node)),
                    self.log,
                )
            else:
                snapshot_pilot_original(self.run_dir, node.id, artifact)
            approval = self._ask(
                "pilot",
                title=f"Approve pilot artifact for {node.id}",
                message=(
                    f"Shape: {node.shape}. Confirm the artifact, or paste your "
                    f"edited version below — the diff will become contract rules.\n\n"
                    f"Artifact:\n\n{_read_artifact(self.run_dir, node.id)[:2400]}"
                ),
                input_label="Edited artifact text (leave empty to approve as-is)",
                context={"node_id": node.id, "shape": node.shape},
            )
            edited = approval.user_input.strip() or _read_artifact(self.run_dir, node.id)
            rule_texts = approve_pilot(self.run_dir, node, edited, self.provider, self.log)
            rules.extend(ContractRule(source=node.id, shape=node.shape, text=text) for text in rule_texts)
        freeze_contract(self.run_dir, rules)

    async def _phase_research(self, *, phase: str = "research") -> None:
        """Dispatch targeted exploration (PLAN.md §A6 second bullet/§C3).

        Two paths, in priority order:

        1. **Operator-supplied** ``options.research_plan`` — unchanged
           from pre-§C3 behavior. The plan is the contract; the harness
           never second-guesses it.
        2. **Auto-planned** (§C3) — when ``options.auto_probe_plan`` is on
           and no explicit plan was supplied, build one from the probe
           planner (windowed ``complete_json`` over candidate leaves).
           This is the "targeted exploration" §A6 names — a model-driven
           decision about which leaves benefit from a probe, gated by a
           deterministic ``needs_probe`` filter and a per-window cap, so
           a model that suggests probes to avoid work is bounded.

        Both paths feed the same ``run_research_loop`` (the existing
        targeted-loop machinery), so findings attach to ``node.inputs``
        exactly the way operator-supplied findings already do.

        PLAN-AUDIT.md §E14: ``phase`` names which ``phase.json`` entry the
        capability-refusal branch below stamps. This method is called two
        ways — as its own top-level phase (``_run_phase("research", ...)``,
        the T3 phase-list entry, where the default ``"research"`` is
        correct and matches what ``_run_phase``'s own tail is about to
        write) and as a sub-step of ``_phase_explore`` while the run is
        still, structurally, in the "explore" phase. The old hardcoded
        ``self._set_phase("research", ...)`` was correct for the first
        caller and a bug for the second: it clobbered ``phase.json``'s
        ``phase`` field with "research" while ``_phase_explore`` (and the
        ``_run_phase("explore", ...)`` frame that invoked it) was still
        running, so a dashboard poll in that window saw ``research/done``
        mid-explore. ``_phase_explore`` now passes ``phase="explore"``.
        """
        plan = self.options.research_plan
        if not plan and self.options.auto_probe_plan:
            plan = self._build_auto_probe_plan()
        if not plan:
            return None
        try:
            await run_research_loop(
                self.run_dir,
                tree_path(self.run_dir),
                plan,
                self.research_adapter_factory,
                self.env,
                EpisodeBudget(),
            )
        except ValueError as exc:  # only capability refusal is a soft miss
            self._set_phase(phase, "done", detail=f"skipped: {exc}")

    def _build_auto_probe_plan(self) -> dict[str, list[ResearchQuery]]:
        """PLAN.md §C3: windowed probe planner over the current tree. The
        plan is the shape ``run_research_loop`` accepts; an empty plan
        (no candidate leaves, or the planner returned nothing) is a no-op
        return, not a skip — ``_phase_research``'s caller may still have
        its own skip logic. Logged as a single harness event so the
        dashboard can show "auto probe planner ran, produced N probes"
        without parsing ``events.jsonl`` for ``node_dispatched`` lines.

        A5-3 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): when the plan call
        already folded probe suggestions into ``probe_plan.json`` (≤2,
        top-level only, written by ``_phase_plan``), those are used instead
        — the separate windowed ``plan_probes`` call is skipped entirely.
        Suggestions naming missing/non-leaf nodes are dropped by
        ``research_plan_from_suggestions``; an empty consumed plan falls
        back to the windowed planner (a stale plan file must not suppress
        a real probe pass)."""
        if not (self.run_dir / "tree.json").exists():
            return {}
        try:
            tree = TaskTree.load(tree_path(self.run_dir))
        except Exception:
            return {}
        from ..v4.probe_planner import ProbeSuggestion, research_plan_from_suggestions

        plan_file = self.run_dir / "probe_plan.json"
        if plan_file.is_file():
            try:
                payload = json.loads(plan_file.read_text(encoding="utf-8"))
                raw = payload.get("probes") if isinstance(payload, dict) else None
            except (OSError, json.JSONDecodeError):
                raw = None
            if isinstance(raw, list):
                suggestions = [
                    ProbeSuggestion(
                        node_id=str(item.get("node_id", "")),
                        slug=str(item.get("slug", "")),
                        question=str(item.get("question", "")),
                        kind=str(item.get("kind") or "web"),
                    )
                    for item in raw
                    if isinstance(item, dict)
                ]
                plan = research_plan_from_suggestions(suggestions, tree)
                if plan:
                    total_probes = sum(len(qs) for qs in plan.values())
                    self._log(
                        {
                            "node_id": "-",
                            "role": "harness",
                            "round": 0,
                            "type": "probe_plan_from_plan_call_consumed",
                            "total_probes": total_probes,
                            "nodes_with_probes": len(plan),
                        }
                    )
                    return plan
        plan = plan_probes(
            tree,
            self.provider,
            on_reasoning=self._reasoning_sink("phase-research"),
            streaming=True,
        )
        total_probes = sum(len(qs) for qs in plan.values())
        self._log(
            {
                "node_id": "-",
                "role": "harness",
                "round": 0,
                "type": "probe_plan_built",
                "total_probes": total_probes,
                "nodes_with_probes": len(plan),
            }
        )
        return plan

    async def _phase_execute(self) -> None:
        """Tier-branched (PLAN.md §A4.3/§B2):

        - **T0** — one direct episode, no ``tree.json`` at all
          (``v6/direct.py:run_direct_episode``). Always returns "done";
          ``_phase_verify`` (T0's own next phase) is what decides
          pass/escalate — folding that into this method too would make
          "execute" spend a call that "verify" is supposed to own.
        - **T1** — no "plan" phase exists to build its one node, so it's
          built directly from the goal, by code
          (``v6/direct.py:build_single_node_tree``), the first time this
          phase runs; then flows through the *unmodified* round loop below.
          Uses a lower ``max_attempts`` than T2/T3 (§A4.4: "T0/T1 node fails
          gates twice with a size defect -> promote to T2") — see
          ``v6/direct.py:DIRECT_MAX_ATTEMPTS``'s docstring for why 2, not 3.
        - **T2/T3** — unchanged from pre-§B2 behavior save one addition:
          the round loop over whatever ``tree.json`` planning already
          produced, but now also wired for runtime split (PLAN.md §A8/
          §B5) via the ``split_handler``/``on_node_passed`` callables
          (``v7/split.py``) — never for T1 (single node, its own
          size-overrun path is ``size_defect_retry`` below, a deliberately
          different move — replan for real at T2 rather than graft a
          partial partition onto a tree that's about to be archived) or
          T0 (no ``tree.json`` to graft into at all). See
          ``v1/round_loop.py``'s module docstring for why these are
          injected callables rather than a direct import there.

        The size-defect escalation check only applies to T1 (T0's own
        check lives in ``_phase_verify``, since T0 never touches
        ``tree.json``/``TaskTree.is_blocked()`` at all). The
        ``split_accepted`` escalation trigger (§A4.4's fourth row) only
        applies to T2 — T3 is already the escalation ceiling that trigger
        targets (``majority_regenerate``'s own driver-side check gates the
        same way).
        """
        tier = self._current_tier()
        if tier == "T0":
            await run_direct_episode(
                self.run_dir,
                self.options.goal.strip(),
                self.provider,
                writer_adapter_factory=self.writer_adapter_factory,
                env=self.env,
                prompt_for_node=lambda node: self._prompt_for_node(node),
                budget=EpisodeBudget(),
                log=self.log,
                max_attempts=DIRECT_MAX_ATTEMPTS,
            )
            return None

        if tier == "T1" and not tree_path(self.run_dir).exists():
            build_single_node_tree(self.options.goal.strip()).save(tree_path(self.run_dir))

        max_attempts = DIRECT_MAX_ATTEMPTS if tier == "T1" else self.options.max_attempts
        # PLAN.md §A8/§B5: runtime split only makes sense against a real,
        # multi-node-capable tree.json — never T1's code-built single-node
        # tree (docstring above).
        enable_split = tier in ("T2", "T3")
        await run_round_loop(
            self.run_dir,
            tree_path(self.run_dir),
            writer_adapter_factory=self.writer_adapter_factory,
            env=self.env,
            provider=self.provider,
            prompt_for_node=lambda node: self._prompt_for_node(
                node, inline_spans=self.options.inline_spans
            ),
            writer_budget_for=lambda node: EpisodeBudget(
                max_duration_seconds=_budget_seconds(node)
            ),
            max_rounds=self.options.max_rounds,
            max_attempts=max_attempts,
            dispatch_policy=self.options.dispatch_policy,
            # PLAN.md §C2: a config, not a redesign — see RunOptions.
            max_parallel=self.options.max_parallel,
            split_handler=handle_split_proposal if enable_split else None,
            on_node_passed=maybe_derive_split_parent if enable_split else None,
            # PLAN-AUDIT.md §E15: the exact same halt.flag check
            # ``_run_phase``'s own phase-boundary halt already reads
            # (``self._halted``) — not a second mechanism — so a halt
            # requested mid-execute stops the round loop from starting new
            # work instead of only taking effect at the next phase
            # boundary.
            should_halt=self._halted,
        )
        tree = self._load_tree()
        if tier == "T1" and tree.is_blocked():
            node = tree.nodes.get(SINGLE_NODE_ID)
            if node is not None and is_size_defect(node.last_defect):
                self._archive_tree_before_replan()
                self._escalate_tier("size_defect_retry", node_id=node.id)
                return None
        if tier == "T2":
            split_parents = sorted(n.id for n in tree.nodes.values() if n.status == "split")
            if split_parents:
                # PLAN.md §A4.4 row 4: "any node's accepted split proposal
                # -> promote T2 -> T3." One trigger per _phase_execute call
                # is enough — escalate() is monotone (v6/tiering.py), and
                # once tier.json says T3 this branch's own `tier == "T2"`
                # guard stops firing on a later resume.
                # §A10: archive T2's spec-derived contract.md before the
                # bump — T3's pilot phase must re-derive contract.md from
                # a real edit-diff, which is strictly richer than the
                # script-rendered one (and without this archive,
                # `_phase_done("pilot")` would see the file and skip the
                # real pilot — same shape as `_archive_tree_before_replan`
                # above for T1 -> T2).
                self._archive_t2_contract_before_pilot()
                self._escalate_tier("split_accepted", node_id=split_parents[0])
                return None
        return None if not tree.is_blocked() else False

    def _archive_tree_before_replan(self) -> None:
        """PLAN.md §A4.4: "T0/T1 node fails gates twice with a size defect
        -> promote to T2, **plan the node's own inputs**." T1's
        ``tree.json`` is one node built directly by code
        (``v6/direct.py:build_single_node_tree``), never the output of a
        real Planner call — but ``_phase_done("plan")`` keys off
        ``tree.json``'s mere *existence*. Escalating without clearing it
        would make T2's "plan" phase believe planning already happened and
        skip straight to re-executing the same stale one-node tree, which
        is exactly the tree whose one node just got escalated away from.
        Renamed aside, not deleted — nothing is discarded (invariant 9) —
        so a real "plan" call can build the actual multi-node tree this
        trigger promises."""
        path = tree_path(self.run_dir)
        if not path.exists():
            return
        archive = path.with_name(f"tree.json.pre-t1-escalation-{int(time.time())}")
        path.rename(archive)

    def _archive_t2_contract_before_pilot(self) -> None:
        """PLAN.md §A10: T2 wrote ``contract.md`` from spec.md by script
        (``_phase_plan``'s §A10 branch calls
        ``v2.contract.render_spec_rubric_to_contract``). When the run
        escalates T2 -> T3 — via ``split_accepted`` (§A4.4 row 4, below) or
        ``majority_regenerate`` (row 3, ``_handle_document_review_triage``)
        — T3's own ``pilot`` phase must re-derive contract.md from its
        edit-diff, which is *strictly richer* than the script-derived one
        (a pilot edit carries operator-style rules the script path can
        never infer). Without this archive, ``_phase_done("pilot")``'s
        ``contract_path(...).exists()`` check would see the T2-scripted
        file and skip the real pilot — the same shape as
        ``_archive_tree_before_replan`` one helper up. Renamed aside, not
        deleted (invariant 9)."""
        path = contract_path(self.run_dir)
        if not path.exists():
            return
        archive = path.with_name(f"contract.md.pre-t2-escalation-{int(time.time())}")
        path.rename(archive)

    async def _phase_verify(self) -> None:
        """T0's dedicated finalize step (PLAN.md §A4.3: T0's phase list is
        ``classify -> execute -> verify``, distinct from T1-3's "review").
        By the time this runs, ``_phase_execute`` already drove the node's
        one Writer episode and its one Reviewer verdict (free — zero
        provider calls — when the node declares no judgment items, exactly
        like every other leaf today) to a terminal status, so the common
        case here is a pure status check with no further dispatch. The only
        case that does real work is resume: a crash between dispatch and
        review leaves the node mid-flight, and ``run_direct_episode`` is
        idempotent/resumable the same way the round loop is, so calling it
        again here safely continues from wherever it stopped.
        """
        node = await run_direct_episode(
            self.run_dir,
            self.options.goal.strip(),
            self.provider,
            writer_adapter_factory=self.writer_adapter_factory,
            env=self.env,
            prompt_for_node=lambda node: self._prompt_for_node(node),
            budget=EpisodeBudget(),
            log=self.log,
            max_attempts=DIRECT_MAX_ATTEMPTS,
        )
        if node.status == "passed":
            return None
        if node.status == "blocked" and is_size_defect(node.last_defect):
            self._escalate_tier("size_defect_retry", node_id=node.id)
            return None
        return False

    async def _phase_review(self) -> None:
        """T1-3's named "review" checkpoint (PLAN.md §A4.3's phase table).
        Per-node review already happened *inside* ``_phase_execute`` —
        ``v1/round_loop.py``'s dispatch and review are coupled per node (a
        node cannot be reviewed before its own gates pass, and splitting
        that into two fully separate global passes over the whole tree is a
        larger restructuring than this workstream's scope). For T1/T3 this
        phase spends nothing further: it just reports whether the tree
        execute produced is blocked.

        **T2 gets one more thing here, unconditionally (PLAN.md §A9's
        table row: "T2: gates + review_node per leaf + one cross-leaf
        consistency pass"):** ``document_review``'s passes 1-3 (coverage,
        duplication, contract-compliance — already windowed over
        promotions, never artifact prose) run every T2 execution, not only
        when the operator opts in via ``RunOptions.document_review``. That
        flag still gates T3's *additional* depth pass (§A11: "T3: as
        today") — this is a difference in what tier T2 structurally *is*,
        not a change to the flag's meaning. ``keep_depth_pass=False`` here
        is what keeps T2 to exactly the 3 windowed passes the table
        promises, without T3's extra per-artifact spot check.

        The triage this pass produces is handled the same way T3's
        flag-gated document review handles its own
        (``_handle_document_review_triage``, factored out so both callers
        share the majority-regenerate escalation guard, the repair
        dispatch, and the audit log line) — except **T2 applies repairs
        without an operator approval gate.** This mirrors §A10's own
        reasoning for why the pilot/contract's ``awaiting_approval`` state
        is T3-only: "a run the operator expected to take four minutes must
        not silently park overnight waiting for a form." A T2 run is the
        cheap/fast tier by construction (no pilot, no contract-freeze
        interview); parking it on a human gate for a check that only ever
        proposes *scoped, gate-and-review-checked* repairs (same guardrail
        as every other repair path — snapshot, gates + review, only then
        overwrite) would reintroduce exactly the overnight-park failure
        mode §A10 was written to avoid, for a tier whose whole point is
        that it doesn't need one. T3 keeps asking, unchanged, because a T3
        run has already paid for a pilot/contract interview and the
        operator is already in the loop for that run.

        An unattributable cross-leaf defect (``review.escalated`` — a
        defect ``document_review`` could not pin to any node id) escalates
        this phase exactly like an unattributable compile failure escalates
        ``assembly_loop.py`` — the operator's `reopen_node` intervention is
        the recovery path, not something this phase can resolve on its
        own.

        PLAN-AUDIT.md §E17: ``_phase_done`` treats "review" as always
        re-run (it's tier-scoped, per the class docstring's ``_ran_key``),
        so a process-level resume calls this method again even though
        nothing about the artifacts changed since the last time it ran.
        Wrapped in ``_document_review_cached_pass`` below, which skips the
        real call entirely when a digest of exactly this pass's inputs
        (every reviewed node's promotion+brief, the frozen contract text,
        and ``keep_depth_pass``) matches what's on disk from the last time
        it ran clean.
        """
        tree = self._load_tree()
        if tree.is_blocked():
            return False
        if self._current_tier() == "T2":
            review = self._document_review_cached_pass(tree, keep_depth_pass=False)
            if review.escalated:
                return False
            await self._handle_document_review_triage(
                review, approval_context="review", require_approval=False
            )
        return None

    def _document_review_digest(self, tree: TaskTree, *, keep_depth_pass: bool) -> str:
        """PLAN-AUDIT.md §E17: a pure function of exactly what the pass
        actually reads — ``build_document_index`` already restricts to
        *passed* nodes, so a still-pending sibling changing status doesn't
        perturb this; only a reviewed node's own ``(promotion, brief)``
        changing (a repair re-dispatched it), the frozen contract being
        amended, or the caller switching ``keep_depth_pass`` (T2 vs T3's
        different scope) can change the digest."""
        entries = build_document_index(self.run_dir, tree)
        contract_text = ""
        try:
            contract_text = contract_path(self.run_dir).read_text(encoding="utf-8")
        except OSError:
            pass
        payload = {
            "entries": [[e.node_id, e.promotion, e.brief] for e in entries],
            "contract": contract_text,
            "keep_depth_pass": keep_depth_pass,
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def _document_review_cache_path(self) -> Path:
        return ensure_audit_dir(self.run_dir) / "document_review.json"

    def _read_document_review_cache(self) -> dict[str, Any]:
        try:
            payload = json.loads(self._document_review_cache_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_document_review_cache(self, digest: str, review: DocumentReviewResult) -> None:
        payload = {
            "digest": digest,
            # "Clean" mirrors what a resuming caller actually needs to
            # know: a digest match over a pass that found real triage (or
            # escalated) must still re-run, since the caller's own repair
            # dispatch didn't happen from a cached result — only a pass
            # that found nothing to do is safe to skip verbatim next time.
            "clean": not review.triage and not review.escalated,
            "ts": time.time(),
        }
        write_text_atomic(
            self._document_review_cache_path(),
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def _document_review_cache_hit(self, digest: str) -> bool:
        cached = self._read_document_review_cache()
        return cached.get("digest") == digest and cached.get("clean") is True

    def _document_review_cached_pass(
        self, tree: TaskTree, *, keep_depth_pass: bool
    ) -> DocumentReviewResult:
        """PLAN-AUDIT.md §E17: the caching wrapper around
        ``run_document_review`` itself — used by ``_phase_review`` (T2,
        unconditional). ``_phase_assemble``'s T3 flag-gated pass runs
        through ``run_assembly_loop`` instead (it dispatches the call
        internally), so it does its own pre-check + cache-write around
        that call rather than through this helper; both write the same
        ``audit/document_review.json`` record via
        ``_write_document_review_cache``."""
        digest = self._document_review_digest(tree, keep_depth_pass=keep_depth_pass)
        if self._document_review_cache_hit(digest):
            self._log(
                {
                    "node_id": "-",
                    "role": "harness",
                    "round": 0,
                    "type": "document_review_cached",
                    "digest": digest,
                }
            )
            return DocumentReviewResult()
        review = run_document_review(
            self.run_dir,
            tree,
            self.provider,
            keep_depth_pass=keep_depth_pass,
            log=self.log,
            on_reasoning=self._reasoning_sink("phase-review"),
            streaming=True,
        )
        self._write_document_review_cache(digest, review)
        return review

    async def _handle_document_review_triage(
        self, review: DocumentReviewResult, *, approval_context: str, require_approval: bool
    ) -> bool:
        """Shared triage-application half of a document-review pass
        (PLAN.md §A9/§B6), factored out because two call sites now run the
        identical windowed cross-leaf pass under different tiers: T2's
        mandatory ``_phase_review`` above (unconditional, no approval
        gate) and T3's flag-gated ``_phase_assemble`` below (unchanged:
        still asks). Handles, in order: the §A4.4 row-3 majority-regenerate
        escalation (tier-gated to T2 *inside* this method via
        ``self._current_tier()``, so calling it from T3 is a correct no-op
        — T3 is already the ceiling that trigger targets); the optional
        operator approval; and dispatching the repairs via
        ``apply_triage``.

        Returns ``True`` iff repairs were actually applied (approval
        granted, or no gate at all) — the one thing that differs between
        the two callers' next step: T3's caller owns the terminal
        concatenation/compile gate and re-runs it against the repaired
        artifacts; T2's caller does not, because ``_phase_assemble`` (which
        owns that) hasn't run yet this round and will pick up the repaired
        files naturally when it does.
        """
        if not review.triage:
            return False
        counts = summarize_triage(review.triage)
        total = sum(counts.values())
        regenerate = counts.get("regenerate", 0)
        tier = self._current_tier()
        if tier == "T2" and total > 0 and regenerate / total >= 0.5:
            # §A10: same as the split_accepted escalation site above —
            # archive T2's spec-derived contract.md before T3's pilot
            # re-derives it from an edit-diff.
            self._archive_t2_contract_before_pilot()
            self._escalate_tier("majority_regenerate", regenerate=regenerate, total=total)
            return False
        if require_approval:
            summary = ", ".join(f"{kind}={count}" for kind, count in counts.items())
            sample = "; ".join(
                f"{node_id} ({triage.classification})"
                for node_id, triage in sorted(review.triage.items())[:8]
            )
            approval = self._ask(
                "document_review",
                title="Document review triage — approve repairs?",
                message=(
                    f"Document-level review found: {summary}.\n\n"
                    f"First entries: {sample}.\n\n"
                    "Repairs dispatch through the same gates as §10 triage "
                    "(snapshot, gates + review, only then overwrite). Reply "
                    "'no' to leave defects in place."
                ),
                context={"phase": approval_context},
            )
            if approval.user_input.strip().lower() in ("n", "no", "abort", "halt"):
                return False
        repaired = await apply_triage(
            self.run_dir,
            triage=serialize_triage(review.triage),
            writer_adapter_factory=self.writer_adapter_factory,
            env=self.env,
            provider=self.provider,
            max_attempts=self.options.max_attempts,
        )
        self._log(
            {
                "node_id": "-",
                "role": "harness",
                "round": 0,
                "type": "document_review_repairs",
                "repaired": repaired,
            }
        )
        return True

    async def _phase_assemble(self) -> None:
        """PLAN.md §A11: T2's assembly is unchanged ("concatenation + index
        + checks + compile, as today") — its cross-leaf consistency pass
        already ran, unconditionally, inside ``_phase_review`` above, so
        asking ``run_assembly_loop`` to run ``document_review`` again here
        for T2 would be a redundant second pass, and worse, one that
        defaults to the depth-pass-enabled shape this table only grants
        T3. Only T3 ever asks ``document_review`` to run from this phase,
        and only when the operator opted in via
        ``RunOptions.document_review`` — byte-identical to pre-§B6
        behavior for T3.

        PLAN-AUDIT.md §E17: unlike ``_phase_review``, this pass runs
        *inside* ``run_assembly_loop`` (it dispatches ``run_document_review``
        itself when ``document_review=True``), so caching happens around
        that call rather than through ``_document_review_cached_pass``: a
        digest match against a clean prior result flips
        ``run_full_document_review`` back to ``False`` before the call
        (skipping it entirely, logging ``document_review_cached``), and a
        real run's result is cached afterward via the same
        ``audit/document_review.json`` record ``_phase_review`` writes."""
        run_full_document_review = (
            self.options.document_review and self._current_tier() == "T3"
        )
        review_digest: str | None = None
        if run_full_document_review:
            review_digest = self._document_review_digest(self._load_tree(), keep_depth_pass=True)
            if self._document_review_cache_hit(review_digest):
                self._log(
                    {
                        "node_id": "-",
                        "role": "harness",
                        "round": 0,
                        "type": "document_review_cached",
                        "digest": review_digest,
                    }
                )
                run_full_document_review = False
        result = await run_assembly_loop(
            self.run_dir,
            tree_path(self.run_dir),
            str(manifest_path(self.run_dir)),
            writer_adapter_factory=self.writer_adapter_factory,
            env=self.env,
            provider=self.provider,
            compile_command=self.options.compile_command,
            writer_budget=EpisodeBudget(),
            max_repairs=3,
            max_attempts=self.options.max_attempts,
            document_review=run_full_document_review,
            workspace_root=self.options.workspace_root,
        )
        if result.escalated:
            return False
        if result.review is not None:
            assert review_digest is not None  # only set when the pass could run
            self._write_document_review_cache(review_digest, result.review)
            # PLAN.md §A4.4's majority-regenerate trigger (checked inside
            # ``_handle_document_review_triage``) never fires from here in
            # practice — ``run_full_document_review`` above is only ever
            # True when the tier is already T3, the ceiling that trigger
            # targets — but the guard lives in the shared helper, not
            # duplicated here, so this stays correct even if that
            # precondition ever changes.
            #
            # Known gap, documented rather than silently glossed over: were
            # this trigger ever to fire from T3 it would be a no-op by
            # construction (escalate() cannot rise above T3); the real
            # majority-regenerate escalation path (T2 -> T3) lives in
            # _phase_review now, and does not itself retroactively
            # re-validate any node already "passed" under T2 against a new
            # contract (that is the existing §10 amend/re-validate
            # machinery, a separate intervention this trigger does not
            # invoke).
            applied = await self._handle_document_review_triage(
                result.review, approval_context="assemble", require_approval=True
            )
            if applied:
                result = await run_assembly_loop(
                    self.run_dir,
                    tree_path(self.run_dir),
                    str(manifest_path(self.run_dir)),
                    writer_adapter_factory=self.writer_adapter_factory,
                    env=self.env,
                    provider=self.provider,
                    compile_command=self.options.compile_command,
                    writer_budget=EpisodeBudget(),
                    max_repairs=3,
                    max_attempts=self.options.max_attempts,
                    workspace_root=self.options.workspace_root,
                )
        return None if not result.escalated else False

    # ------------------------------------------------------------------
    # Human gates (disk approvals)
    # ------------------------------------------------------------------
    def _ask(
        self,
        kind: str,
        *,
        title: str,
        message: str,
        input_label: str = "",
        context: dict[str, Any] | None = None,
        questions: list[dict[str, str]] | None = None,
    ) -> Any:
        """Create (or reuse the unanswered) pending approval for (kind,
        context) and block until any surface resolves it. ``questions``
        (PLAN.md §A5/§B3) turns this into a batch-form approval — every
        question of one intake round in a single record — instead of the
        plain single free-text prompt every other ``kind`` still uses."""
        context = context or {}
        existing = approval_store.find_pending(self.run_dir, kind=kind, **context)
        approval = existing or self._create_approval(
            kind, title=title, message=message, input_label=input_label,
            context=context, questions=questions,
        )
        current_phase = _read_phase(self.run_dir).get("phase", "intake")
        extra = f" ({context['round']})" if context and "round" in context else ""
        self._set_phase(current_phase, "waiting_for_approval", detail=f"Waiting for approval: {title}{extra}")
        try:
            res = approval_store.wait_for_resolution(
                self.run_dir, approval.approval_id, poll_interval=self.poll_interval
            )
        finally:
            self._set_phase(current_phase, _IN_PROGRESS, detail="")
        return res

    def _create_approval(
        self, kind: str, *, title: str, message: str, input_label: str,
        context: dict[str, Any], questions: list[dict[str, str]] | None = None,
    ):
        record = approval_store.Approval.create(
            kind, title=title, message=message, input_label=input_label, context=context,
            questions=questions,
        )
        approval_store.append(self.run_dir, record)
        self._log(
            {
                "node_id": "-",
                "role": "harness",
                "round": 0,
                "type": "approval_requested",
                "approval_id": record.approval_id,
                "kind": kind,
            }
        )
        return record

    # ------------------------------------------------------------------
    # Durable helpers
    # ------------------------------------------------------------------
    def _phase_done(self, phase: str) -> bool:
        if not self._has_run_dir():
            return False
        if phase == "classify":
            return tier_path(self.run_dir).exists()
        if phase == "intake":
            try:
                return "## Goal" in spec_path(self.run_dir).read_text(encoding="utf-8")
            except OSError:
                return False
        if phase in ("survey", "explore"):
            return (self.run_dir / "spine.json").exists()
        if phase == "plan":
            return tree_path(self.run_dir).exists()
        if phase == "pilot":
            return contract_path(self.run_dir).exists()
        return False  # execute/verify/review/research/assemble: idempotent, always re-run

    def _has_run_dir(self) -> bool:
        return self.run_dir.exists() and (self.run_dir / "events.jsonl").exists()

    def _set_phase(self, phase: str, status: str, detail: str = "") -> None:
        payload = {"phase": phase, "status": status, "detail": detail, "ts": time.time()}
        phase_path(self.run_dir).write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
        )
        # B2-3: every phase transition refreshes the heartbeat — a phase
        # boundary is the moment liveness most needs to know the driver is
        # still alive. (The heartbeat thread covers the long waits between
        # boundaries.)
        record_heartbeat(self.run_dir)

    def _append_explorer_reasoning(self, node_id: str, text: str) -> None:
        """Append one ``{"type": "reasoning", ...}`` line to the explorer
        pseudo-agent's trace.jsonl -- ``rendering.parse_trace`` already
        turns that exact type into a "thinking" chat entry (same code path
        a real gptme trace uses), so the dashboard's existing Chat tab
        renders it with no changes on that side. Not fsync'd like
        events.jsonl: a lost reasoning line on a crash is cosmetic, unlike
        losing which node was dispatched."""
        trace_path = ensure_node_trace_path(self.run_dir, node_id)
        with trace_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "reasoning", "content": text}, ensure_ascii=False) + "\n")

    def _halted(self) -> bool:
        return halt_path(self.run_dir).exists()

    def _load_tree(self) -> TaskTree:
        """§11.6: a tree.json that exists but cannot be parsed is a corrupt
        durable file, not an empty tree. Swallowing the error made a
        kill -9 mid-save resume as "zero nodes" while ``_phase_done("plan")``
        still returned True — the run converged on an empty assembly.
        Missing file (plan phase never ran) is the only empty-tree case."""
        path = tree_path(self.run_dir)
        if not path.exists():
            return TaskTree(nodes={})
        return TaskTree.load(path)

    def _log(self, event: dict[str, Any]) -> None:
        self.log.append(event)

    def _write_source_and_spec(self) -> None:
        # §11.9: source.txt is protected on resume — an operator who fixed
        # the corpus by hand keeps the fix; only a fresh run (no source.txt
        # yet) writes it.
        if not source_path(self.run_dir).exists():
            write_text_atomic(source_path(self.run_dir), self.options.source_text)
        spec = run_spec_path(self.run_dir)
        if not spec.exists():
            write_text_atomic(
                spec,
                json.dumps(self.options.to_spec(), ensure_ascii=False, indent=2) + "\n",
            )

    def _default_writer_factory(self) -> WriterAdapterFactory:
        def factory(node: TaskNode) -> AgentAdapter:
            from .backends import build_writer_adapter

            work = self.options.work_object
            # PLAN.md §A3 point 1: kind="workspace" means the Writer's cwd
            # becomes the real repo, not the run directory — the change
            # that makes coding tasks reachable at all. Run-dir bookkeeping
            # (prompt_dir, out/, scratch/, trace.jsonl) all stay anchored to
            # self.run_dir (already absolute, §D0b) regardless of this
            # branch; only the adapter's cwd moves.
            workspace_path = (
                work.root
                if work is not None and work.kind == "workspace" and work.root is not None
                else self.run_dir
            )
            return build_writer_adapter(
                self.options.backend,
                workspace_path=workspace_path,
                prompt_dir=self.run_dir / "tmp" / "prompts",
                node=node,
                model=self.options.model,
                run_dir=self.run_dir,
            )

        return factory

    def _writer_workspace_path(self) -> Path:
        """The Writer's cwd for the current run — ``work.root`` for
        ``kind="workspace"``, else ``run_dir`` itself (PLAN.md §A3). Shared
        by ``_default_writer_factory`` and ``_prompt_for_node`` so the
        adapter's ``hidden_paths`` and the prompt's notice always agree."""
        work = self.options.work_object
        if work is not None and work.kind == "workspace" and work.root is not None:
            return Path(work.root)
        return Path(self.run_dir)

    def _prompt_for_node(
        self, node: TaskNode, *, inline_spans: bool | None = None
    ) -> str:
        """``build_node_prompt`` with the run's hidden-paths pair injected
        (PLAN-AUDIT-COST §A6-1): the same (hidden, exceptions) tuples
        ``build_writer_adapter`` hands the episode's adapter, so the notice
        renders once, in the stable region, instead of being appended by
        ``cli_agent.run_episode`` after all per-node content."""
        from .backends import hidden_paths_for_node

        hidden, exceptions = hidden_paths_for_node(
            node, self.run_dir, self._writer_workspace_path()
        )
        kwargs = dict(
            hidden_paths=hidden,
            hidden_path_exceptions=exceptions,
        )
        if inline_spans is not None:
            kwargs["inline_spans"] = inline_spans
        return build_node_prompt(node, self.run_dir, **kwargs)

    def _default_research_factory(self) -> ResearchAdapterFactory:
        def factory(node: TaskNode, query: ResearchQuery) -> AgentAdapter:
            from .backends import build_research_adapter

            return build_research_adapter(
                self.options.backend,
                workspace_path=self._probe_workspace_path(query),
                prompt_dir=self.run_dir / "tmp" / "prompts",
                query=query,
                model=self.options.model,
                run_dir=self.run_dir,
            )

        return factory

    def _default_probe_adapter_factory(self) -> ProbeAdapterFactory:
        """PLAN.md §A6/§B4: builds the adapter for one structural-exploration
        probe (``_run_structural_exploration``, above). A separate factory
        from ``research_adapter_factory`` because that one's shape —
        ``Callable[[TaskNode, ResearchQuery], AgentAdapter]`` — is tied to a
        real, already-planned ``TaskNode``; a structural-exploration probe
        runs *before* planning even starts, over a ``SpineUnit``, not a
        node. Kept as its own overridable driver attribute (not inlined
        into ``_run_structural_exploration``) so a test can inject a
        call-counting fake the same way ``writer_adapter_factory``/
        ``research_adapter_factory`` already can."""

        def factory(query: ResearchQuery) -> AgentAdapter:
            from .backends import build_research_adapter

            return build_research_adapter(
                self.options.backend,
                workspace_path=self._probe_workspace_path(query),
                prompt_dir=self.run_dir / "tmp" / "prompts",
                query=query,
                model=self.options.model,
                run_dir=self.run_dir,
            )

        return factory

    def _probe_workspace_path(self, query: ResearchQuery) -> Path:
        """PLAN.md §A6/§B4: a ``workspace``-kind probe's cwd must actually be
        (or be able to read into) the work object's root, not the run
        directory — mirroring ``_default_writer_factory``'s existing
        ``kind="workspace"`` branch above. Every other kind (``web``,
        ``corpus``, ``doc_retrieval``) keeps today's behavior: the run
        directory (``corpus`` reads ``spine/`` there; ``web`` and
        ``doc_retrieval`` get no filesystem tools at all, so this is moot
        for them)."""
        work = self._effective_work_object()
        if query.kind == "workspace" and work.kind == "workspace" and work.root is not None:
            return work.root
        return self.run_dir


def _local_env(run_dir: Path) -> Environment:
    from ..environment.local import LocalEnvironment

    return LocalEnvironment(tmp_dir=str(run_dir / "tmp"))


def _read_artifact(run_dir: Path, node_id: str) -> str:
    path = node_artifact_path(run_dir, node_id)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _read_phase(run_dir: Path) -> dict[str, Any]:
    try:
        payload = json.loads(phase_path(run_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _count_statuses(tree: TaskTree) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in tree.nodes.values():
        counts[node.status] = counts.get(node.status, 0) + 1
    return counts


def escalate_run(run_dir: str | Path) -> dict[str, Any]:
    """PLAN.md §A4.4 "Operator escalate intervention -> promote one tier."
    §B2's third (of four) wired escalation trigger — the CLI's ``escalate``
    subcommand (``pipeline/cli.py``) is its only caller today. Mirrors
    ``amend_contract_and_estimate``'s shape: a pure, synchronous
    read-modify-write over the run directory, no provider call, no writer
    dispatch. The phase loop picks up the grown phase list the next time
    ``RecursiveDriver.run()`` executes (invariant 9: tiers only rise, so
    this composes with resume for free — there is no separate "apply the
    escalation" step)."""
    run_dir = Path(run_dir)
    path = tier_path(run_dir)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FileNotFoundError(
            f"no tier.json at {path} — has the classify phase run yet?"
        ) from exc
    if not isinstance(record, dict) or record.get("tier") not in ("T0", "T1", "T2", "T3"):
        raise ValueError(f"tier.json at {path} has no valid tier: {record!r}")
    current: Tier = record["tier"]
    new_tier = escalate(current, "operator")
    record["tier"] = new_tier
    record["ts"] = time.time()
    write_text_atomic(path, json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    EventLog(events_path(run_dir)).append(
        {
            "node_id": "-",
            "role": "operator",
            "round": 0,
            "type": "run_tier_escalated",
            "trigger": "operator",
            "from": current,
            "to": new_tier,
        }
    )
    return {"from": current, "to": new_tier}


# ----------------------------------------------------------------------
# Post-run interventions (PLAN.md §10): contract amendment -> re-validation
# triage, then apply; reopen one passed node as a scoped repair.
# ----------------------------------------------------------------------
def amend_contract_and_estimate(
    run_dir: str | Path,
    *,
    rule_text: str,
    reason: str,
    prefilter: bool = True,
) -> dict[str, Any]:
    """§10 contract amendment, phase 1 — **zero provider calls**: append
    the rule and compute the re-validation cost estimate, so the operator
    sees the token price before any Reviewer spends a single one (§10:
    "Show the cost estimate and the counts, get approval, then execute";
    §11.10.4: the estimate shown *after* the pass ran was theater).

    Returns ``{contract, estimate}``; the reviewer pass is a second,
    separately-gated call (``run_amendment_revalidation``)."""
    from ..v2.contract import amend_contract
    from ..v3.revalidate import estimate_revalidation_cost

    run_dir = Path(run_dir)
    contract_text = amend_contract(run_dir, rule_text, reason=reason)
    tree = TaskTree.load(tree_path(run_dir))
    estimate = estimate_revalidation_cost(
        run_dir, tree, contract_text, amendment_text=rule_text, prefilter=prefilter
    )
    return {
        "contract": contract_text,
        "estimate": {
            "nodes": estimate.node_count,
            "skipped": estimate.skipped_count,
            "tokens": estimate.estimated_tokens,
        },
    }


def run_amendment_revalidation(
    run_dir: str | Path,
    *,
    contract_text: str,
    rule_text: str,
    provider: OpenAICompatibleProvider,
    prefilter: bool = True,
) -> dict[str, Any]:
    """§10 contract amendment, phase 2 — the read-only re-validation pass
    itself, gated separately from phase 1 so the estimate can be shown
    before these Reviewer tokens are spent. Returns ``{counts, triage}``.
    No writer runs here."""
    from ..v3.revalidate import run_revalidation_pass, summarize_triage

    run_dir = Path(run_dir)
    tree = TaskTree.load(tree_path(run_dir))
    triage_by_node = run_revalidation_pass(
        run_dir, tree, tree_path(run_dir), contract_text, provider,
        amendment_text=rule_text, prefilter=prefilter,
    )
    return {
        "counts": summarize_triage(triage_by_node),
        "triage": {
            node_id: {
                "classification": triage.classification,
                "verdict": triage.verdict.verdict,
                "items": triage.verdict.items,
            }
            for node_id, triage in triage_by_node.items()
        },
    }


async def amend_and_revalidate(
    run_dir: str | Path,
    *,
    rule_text: str,
    reason: str,
    provider: OpenAICompatibleProvider,
    prefilter: bool = True,
) -> dict[str, Any]:
    """§10 contract amendment, both phases in one call (for callers that do
    their own gating). Prefer the two-phase pair above — the estimate must
    reach the operator before ``run_amendment_revalidation`` spends the
    Reviewer tokens it prices."""
    phase1 = amend_contract_and_estimate(
        run_dir, rule_text=rule_text, reason=reason, prefilter=prefilter
    )
    phase2 = run_amendment_revalidation(
        run_dir,
        contract_text=phase1["contract"],
        rule_text=rule_text,
        provider=provider,
        prefilter=prefilter,
    )
    return {**phase1, **phase2}


async def apply_triage(
    run_dir: str | Path,
    *,
    triage: dict[str, Any],
    writer_adapter_factory: WriterAdapterFactory,
    env: Environment,
    provider: OpenAICompatibleProvider,
    max_attempts: int = 3,
) -> list[str]:
    """§10 second half: dispatch a repair (patch or regenerate, per its
    triage) for every non-clean node. Returns the ids repaired."""
    from ..v3.revalidate import Triage

    run_dir = Path(run_dir)
    tree = TaskTree.load(tree_path(run_dir))
    triage_by_node: dict[str, Triage] = {}
    for node_id, record in triage.items():
        if not isinstance(record, dict):
            continue
        verdict = ReviewVerdict(
            node_id=node_id,
            verdict=str(record.get("verdict", "fail")),
            items=list(record.get("items") or []),
        )
        triage_by_node[node_id] = Triage(
            node_id=node_id,
            classification=str(record.get("classification", "regenerate")),
            verdict=verdict,
        )
    log = EventLog(events_path(run_dir))
    outcomes = await apply_revalidation_triage(
        run_dir,
        tree,
        tree_path(run_dir),
        str(manifest_path(run_dir)),
        triage_by_node,
        writer_adapter_factory,
        env,
        provider,
        log,
        writer_budget=EpisodeBudget(),
        max_attempts=max_attempts,
    )
    return [outcome.node_id for outcome in outcomes]


async def reopen_node(
    run_dir: str | Path,
    *,
    node_id: str,
    defect: str,
    writer_adapter_factory: WriterAdapterFactory,
    env: Environment,
    provider: OpenAICompatibleProvider,
    max_attempts: int = 3,
) -> list[str]:
    """§10 "Reopen node" intervention: mark one passed node stale and
    dispatch a single scoped repair from the operator's defect text — the
    smallest blast radius (§10 table)."""
    run_dir = Path(run_dir)
    tree = TaskTree.load(tree_path(run_dir))
    if node_id not in tree.nodes:
        raise KeyError(f"unknown node: {node_id!r}")
    node = tree.nodes[node_id]
    if node.status != "passed":
        raise ValueError(f"node {node_id!r} is {node.status!r}, not 'passed' — nothing to reopen")
    verdict = ReviewVerdict(
        node_id=node_id,
        verdict="fail",
        items=[{"id": "reopen", "pass": False, "defect": defect, "class": "patchable"}],
    )
    return await apply_triage(
        run_dir,
        triage={
            node_id: {
                "classification": "patchable",
                "verdict": verdict.verdict,
                "items": verdict.items,
            }
        },
        writer_adapter_factory=writer_adapter_factory,
        env=env,
        provider=provider,
        max_attempts=max_attempts,
    )