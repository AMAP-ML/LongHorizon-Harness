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
import json
import time
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
    estimate_scope,
    max_explorers_for,
    measure_signals,
    phases_for,
    tier_max,
)
from ..v6.work_object import WorkObject, survey_workspace, work_object_from_text, work_object_none
from ..v0.events import EventLog
from ..v0.run_dir import write_text_atomic
from ..v0.run_dir import create_run_dir, ensure_node_trace_path, events_path, manifest_path, node_artifact_path, spec_path
from ..v1.provider import OpenAICompatibleProvider
from ..v1.reviewer import ReviewVerdict
from ..v1.round_loop import run_round_loop
from ..v1.tree import TaskNode, TaskTree
from ..v2.contract import ContractRule, freeze_contract
from ..v2.intake import GlobalRubric, IntakeObjection, IntakeQuestion, render_spec_md, run_intake
from ..v2.pilot import (
    approve_pilot,
    run_pilot,
    select_pilot_nodes,
    snapshot_pilot_original,
)
from ..v2.planner import build_tree
from ..v2.retrieval import build_chunk_index
from ..v2.run_dir import contract_path
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
from ..v3.revalidate import Triage, apply_revalidation_triage, run_revalidation_pass
from ..v4.research import ResearchQuery, run_research_query
from ..v4.research_loop import run_research_loop
from ..v4.run_dir import research_finding_path
from . import approvals as approval_store
from .backends import parse_research_plan
from .liveness import record_driver_start
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
    document_review: bool = False
    survey_mode: str = "model"
    inline_spans: bool = False
    # PLAN.md §A4.4/§B2: a floor, never a ceiling (invariant 9) — the
    # classifier still runs and still measures a real tier; the tier
    # actually used for phases_for() is max(measured, tier_override).
    # One of "T0"/"T1"/"T2"/"T3", or None for "no forced floor".
    tier_override: str | None = None

    def to_spec(self) -> dict[str, Any]:
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
            "document_review": self.document_review,
            "survey_mode": self.survey_mode,
            "inline_spans": self.inline_spans,
            "tier_override": self.tier_override,
        }
        # §11.10.7: the corpus lives once, in source.txt. Embedding
        # source_text here duplicated the run's largest file into a JSON
        # every resume re-parses (*and* into the --detach argv, see
        # cli.py); the spec records where the corpus is instead. Legacy
        # specs that do embed it still load (from_spec reads the field).
        spec["source"] = "source.txt"
        return spec

    @staticmethod
    def from_spec(data: dict[str, Any]) -> "RunOptions":
        return RunOptions(
            goal=str(data.get("goal", "")),
            backend=str(data.get("backend", "gptme")),
            model=data.get("model"),
            # Legacy specs embedded source_text; spec["source"] just names
            # the file in the run dir, which resume never needs to re-read —
            # source.txt already exists (driver only writes it when missing).
            source_text=str(data.get("source_text", "")),
            compile_command=data.get("compile_command"),
            research_plan=parse_research_plan(data.get("research_plan")),
            max_rounds=int(data.get("max_rounds", 100)),
            max_attempts=int(data.get("max_attempts", 3)),
            dispatch_policy=str(data.get("dispatch_policy", "model")),
            document_review=bool(data.get("document_review", False)),
            survey_mode=str(data.get("survey_mode", "model")),
            inline_spans=bool(data.get("inline_spans", False)),
            tier_override=data.get("tier_override"),
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
        # run_dir stays relative (the dashboard's default runs_root is
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
                    self._set_phase(_HALTED, detail=f"halted before {next_phase}")
                    self._log({"node_id": "-", "role": "harness", "round": round_index, "type": "halting"})
                    report = RunReport(status="halted", phase=next_phase, detail="halted by operator")
                    break
                report = await self._run_phase(next_phase, round_index=round_index)
                round_index += 1
                ran.add(next_key)
                if report.status != "done":
                    break
        report = report or RunReport(status="done", phase=phases_for(self._current_tier())[-1])
        report.tree_counts = _count_statuses(self._load_tree())
        # §11.9: run_completed is a claim about the run's outcome — logging
        # it after a halt/escalate/error made the event log disagree with
        # the report.
        if report.status == "done":
            self._log({"node_id": "-", "role": "harness", "round": 0, "type": "run_completed"})
        return report

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
        
        max_auto_attempts = 3
        attempt = 0
        while True:
            attempt += 1
            if self._halted():
                self._set_phase(_HALTED, detail=f"halted in {phase}")
                return RunReport(status="halted", phase=phase, detail="halted by operator")
            try:
                outcome: Any = await getattr(self, f"_phase_{phase}")()
                break
            except Exception as exc:  # noqa: BLE001 — the phase boundary is the reporter
                if is_rate_limit_or_busy_error(exc) or attempt >= max_auto_attempts:
                    self._set_phase(phase, "error", detail=str(exc))
                    self._log(
                        {"node_id": "-", "role": "harness", "round": round_index, "type": "phase_failed", "phase": phase, "error": str(exc)}
                    )
                    return RunReport(status="error", phase=phase, detail=str(exc))
                # Auto-resume non-429/501 error automatically
                err_msg = str(exc)
                self._set_phase(phase, _IN_PROGRESS, detail=f"Auto-resuming (attempt {attempt}/{max_auto_attempts}) after error: {err_msg}")
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
                await asyncio.sleep(1.0)
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
        """
        work = self._effective_work_object()
        goal = self.options.goal.strip()
        if not goal:
            raise ValueError("goal is required")
        signals = measure_signals(goal, work)
        override = (self.options.tier_override or "").upper() or None
        if override == "T3":
            estimate = ScopeEstimate()
            measured: Tier = "T3"
        else:
            estimate = estimate_scope(goal, work, self.provider)
            measured = classify(signals, estimate)
        tier: Tier = tier_max(measured, override) if override else measured
        payload = {
            "tier": tier,
            "measured_tier": measured,
            "override": override,
            "signals": asdict(signals),
            "estimate": asdict(estimate),
            "needs_intake": bool(estimate.ambiguities or estimate.objections),
            "needs_explore": not estimate.answerable_without_exploration,
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
        estimate = self._read_tier_record().get("estimate") or {}
        ambiguities = [str(item) for item in (estimate.get("ambiguities") or [])]
        objections = [str(item) for item in (estimate.get("objections") or [])]
        run_intake(self.run_dir, goal, ambiguities, objections, self.provider, self._ask_intake_round)

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
            context={"round": round_index},
            questions=[{"id": question.id, "text": question.text} for question in questions],
        )
        return dict(approval.answers)

    async def _phase_survey(self) -> None:
        from ..v2.embeddings import embeddings_available
        from ..v2.survey import survey_chunks_deterministic

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
                    votes = survey_chunks(chunks, self.provider, on_reasoning=on_reasoning)
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
        tier = self._current_tier()
        if tier in ("T2", "T3"):
            await self._run_structural_exploration(tier)
        if self.options.research_plan:
            await self._phase_research()

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
        tree = build_tree(
            load_spine(self.run_dir),
            self.provider,
            input_path_for=lambda unit: unit_input_path(self.run_dir, unit),
            log=self.log,
            unit_summary_for=self._explore_summary_for,
        )
        tree.save(tree_path(self.run_dir))

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
                    build_node_prompt(node, self.run_dir),
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

    async def _phase_research(self) -> None:
        if not self.options.research_plan:
            return None
        try:
            await run_research_loop(
                self.run_dir,
                tree_path(self.run_dir),
                self.options.research_plan,
                self.research_adapter_factory,
                self.env,
                EpisodeBudget(),
            )
        except ValueError as exc:  # only capability refusal is a soft miss
            self._set_phase("research", "done", detail=f"skipped: {exc}")

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
        - **T2/T3** — unchanged from pre-§B2 behavior: the round loop over
          whatever ``tree.json`` planning already produced.

        The size-defect escalation check only applies to T1 (T0's own
        check lives in ``_phase_verify``, since T0 never touches
        ``tree.json``/``TaskTree.is_blocked()`` at all).
        """
        tier = self._current_tier()
        if tier == "T0":
            await run_direct_episode(
                self.run_dir,
                self.options.goal.strip(),
                self.provider,
                writer_adapter_factory=self.writer_adapter_factory,
                env=self.env,
                prompt_for_node=lambda node: build_node_prompt(node, self.run_dir),
                budget=EpisodeBudget(),
                log=self.log,
                max_attempts=DIRECT_MAX_ATTEMPTS,
            )
            return None

        if tier == "T1" and not tree_path(self.run_dir).exists():
            build_single_node_tree(self.options.goal.strip()).save(tree_path(self.run_dir))

        max_attempts = DIRECT_MAX_ATTEMPTS if tier == "T1" else self.options.max_attempts
        await run_round_loop(
            self.run_dir,
            tree_path(self.run_dir),
            writer_adapter_factory=self.writer_adapter_factory,
            env=self.env,
            provider=self.provider,
            prompt_for_node=lambda node: build_node_prompt(
                node, self.run_dir, inline_spans=self.options.inline_spans
            ),
            writer_budget_for=lambda node: EpisodeBudget(
                max_duration_seconds=_budget_seconds(node)
            ),
            max_rounds=self.options.max_rounds,
            max_attempts=max_attempts,
            dispatch_policy=self.options.dispatch_policy,
        )
        tree = self._load_tree()
        if tier == "T1" and tree.is_blocked():
            node = tree.nodes.get(SINGLE_NODE_ID)
            if node is not None and is_size_defect(node.last_defect):
                self._archive_tree_before_replan()
                self._escalate_tier("size_defect_retry", node_id=node.id)
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
            prompt_for_node=lambda node: build_node_prompt(node, self.run_dir),
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
        larger restructuring than this workstream's scope). This phase is
        the confirmation checkpoint the table's phase name promises: it
        spends nothing further, it just reports whether the tree that
        execute produced is blocked."""
        tree = self._load_tree()
        return None if not tree.is_blocked() else False

    async def _phase_assemble(self) -> None:
        from ..v3.document_review import serialize_triage
        from ..v3.revalidate import summarize_triage

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
            document_review=self.options.document_review,
        )
        if result.escalated:
            return False
        if result.review is not None and result.review.triage:
            counts = summarize_triage(result.review.triage)
            # PLAN.md §A4.4: "Reviewer returns class: regenerate on >= half
            # of a T2 plan's leaves -> promote to T3, re-pilot." Checked
            # here, not inside v3/document_review.py itself, for the same
            # reason the size-defect check lives in the driver rather than
            # v1/round_loop.py — document_review is tier-agnostic and used
            # by T3 too, where this trigger must never fire (T3 is already
            # the ceiling). Escalating supersedes this pass's own
            # repair-approval flow entirely: once tier.json says T3, the
            # next iteration of run()'s phase loop picks up "pilot" (T2
            # never ran it) and re-enters execute/review/assemble under the
            # freshly-frozen contract, so asking approval for a T2-scoped
            # repair here would just be discarded work.
            #
            # Known gap, documented rather than silently glossed over: this
            # escalates and re-pilots, but does not itself retroactively
            # re-validate T2's already-"passed" leaves against the new
            # contract (that is the existing §10 amend/re-validate
            # machinery, a separate intervention this trigger does not
            # invoke). The trigger fires and the tier rises correctly; full
            # retroactive contract enforcement on old leaves is not part of
            # what §B2 wires.
            total = sum(counts.values())
            regenerate = counts.get("regenerate", 0)
            tier = self._current_tier()
            if tier == "T2" and total > 0 and regenerate / total >= 0.5:
                self._escalate_tier(
                    "majority_regenerate",
                    regenerate=regenerate,
                    total=total,
                )
                return None
            summary = ", ".join(f"{kind}={count}" for kind, count in counts.items())
            sample = "; ".join(
                f"{node_id} ({triage.classification})"
                for node_id, triage in sorted(result.review.triage.items())[:8]
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
                context={"phase": "assemble"},
            )
            if approval.user_input.strip().lower() in ("n", "no", "abort", "halt"):
                return None
            repaired = await apply_triage(
                self.run_dir,
                triage=serialize_triage(result.review.triage),
                writer_adapter_factory=self.writer_adapter_factory,
                env=self.env,
                provider=self.provider,
                max_attempts=self.options.max_attempts,
            )
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