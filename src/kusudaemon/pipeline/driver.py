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
from ..v0.events import EventLog
from ..v0.run_dir import write_text_atomic
from ..v0.run_dir import create_run_dir, events_path, manifest_path, node_artifact_path, spec_path
from ..v1.provider import OpenAICompatibleProvider
from ..v1.reviewer import ReviewVerdict
from ..v1.round_loop import run_round_loop
from ..v1.tree import TaskNode, TaskTree
from ..v2.contract import ContractRule, freeze_contract
from ..v2.intake import run_intake
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
from ..v4.research import ResearchQuery
from ..v4.research_loop import run_research_loop
from . import approvals as approval_store
from .backends import parse_research_plan
from .prompts import build_node_prompt
from .run_dir import halt_path, phase_path, run_spec_path, source_path, tree_path

PHASES = ("intake", "survey", "plan", "pilot", "research", "execute", "assemble")

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
    compile_command: str | None = None
    research_plan: dict[str, list[ResearchQuery]] = field(default_factory=dict)
    max_rounds: int = 100
    max_attempts: int = 3
    dispatch_policy: str = "model"
    document_review: bool = False
    survey_mode: str = "model"
    inline_spans: bool = False

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
        poll_interval: float = 1.0,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.provider = provider
        self.options = options
        self.env = env or _local_env(self.run_dir)
        self.writer_adapter_factory = writer_adapter_factory or self._default_writer_factory()
        self.research_adapter_factory = research_adapter_factory or self._default_research_factory()
        self.poll_interval = poll_interval
        create_run_dir(self.run_dir.parent, self.run_dir.name)
        self._write_source_and_spec()
        self.log = EventLog(events_path(self.run_dir))

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------
    async def run(self) -> RunReport:
        report: RunReport | None = None
        for index, phase in enumerate(PHASES):
            if self._halted():
                self._set_phase(_HALTED, detail=f"halted before {phase}")
                self._log({"node_id": "-", "role": "harness", "round": index, "type": "halting"})
                report = RunReport(status="halted", phase=phase, detail="halted by operator")
                break
            report = await self._run_phase(phase, round_index=index)
            if report.status != "done":
                break
        report = report or RunReport(status="done", phase=PHASES[-1])
        report.tree_counts = _count_statuses(self._load_tree())
        # §11.9: run_completed is a claim about the run's outcome — logging
        # it after a halt/escalate/error made the event log disagree with
        # the report.
        if report.status == "done":
            self._log({"node_id": "-", "role": "harness", "round": 0, "type": "run_completed"})
        return report

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
                self._log(
                    {
                        "node_id": "-",
                        "role": "harness",
                        "round": round_index,
                        "type": "phase_auto_resuming",
                        "phase": phase,
                        "attempt": attempt,
                        "error": str(exc),
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
    async def _phase_intake(self) -> None:
        goal = self.options.goal.strip()
        if not goal:
            raise ValueError("goal is required")
        run_intake(self.run_dir, goal, self.provider, self._answer_intake)

    def _answer_intake(self, question: str, dimension: str) -> str:
        """§11.9: the approval is keyed on the rubric dimension, so a resume
        that restarts the question loop reuses *that dimension's* pending
        record instead of handing the next-dimension question whatever
        answer is still sitting in the file."""
        approval = self._ask(
            "intake_question",
            title="Intake question",
            message=question,
            input_label="Your answer",
            context={"dimension": dimension},
        )
        return approval.user_input.strip()

    async def _phase_survey(self) -> None:
        from ..v2.embeddings import embeddings_available
        from ..v2.survey import survey_chunks_deterministic

        source = source_path(self.run_dir).read_text(encoding="utf-8").strip()
        if not source:
            units = [SpineUnit(id="unit-01", label="The goal", start_chunk=0, end_chunk=0, tokens=0)]
        else:
            chunks = chunk_text(source)
            if self.options.survey_mode == "embedding" and embeddings_available():
                votes = survey_chunks_deterministic(chunks)
            else:
                if self.options.survey_mode == "embedding":
                    # Loud but non-fatal (PLAN-zeromem.md §3.7): the operator
                    # paid for 250 calls they meant to avoid, but a missing
                    # optional extra is a config problem, not a corpus
                    # problem. Logged to the append-only event log — never
                    # phase.json, whose detail field is clobbered by
                    # _run_phase's tail call.
                    self._log(
                        {
                            "node_id": "-",
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
                votes = survey_chunks(chunks, self.provider)
            units = assemble_spine(chunks, votes)
            materialize_units(self.run_dir, chunks, units)
            build_chunk_index(self.run_dir, chunks, units)
        save_spine(self.run_dir, units)

    async def _phase_plan(self) -> None:
        tree = build_tree(
            load_spine(self.run_dir),
            self.provider,
            input_path_for=lambda unit: unit_input_path(self.run_dir, unit),
            log=self.log,
        )
        tree.save(tree_path(self.run_dir))

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
            max_attempts=self.options.max_attempts,
            dispatch_policy=self.options.dispatch_policy,
        )
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
    ) -> Any:
        """Create (or reuse the unanswered) pending approval for (kind,
        context) and block until any surface resolves it."""
        context = context or {}
        existing = approval_store.find_pending(self.run_dir, kind=kind, **context)
        approval = existing or self._create_approval(kind, title=title, message=message, input_label=input_label, context=context)
        current_phase = _read_phase(self.run_dir).get("phase", "intake")
        dim_info = f" ({context['dimension']})" if context and "dimension" in context else ""
        self._set_phase(current_phase, "waiting_for_approval", detail=f"Waiting for approval: {title}{dim_info}")
        try:
            res = approval_store.wait_for_resolution(
                self.run_dir, approval.approval_id, poll_interval=self.poll_interval
            )
        finally:
            self._set_phase(current_phase, _IN_PROGRESS, detail="")
        return res

    def _create_approval(self, kind: str, *, title: str, message: str, input_label: str, context: dict[str, Any]):
        record = approval_store.Approval.create(
            kind, title=title, message=message, input_label=input_label, context=context
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
        if phase == "intake":
            try:
                return "## Goal" in spec_path(self.run_dir).read_text(encoding="utf-8")
            except OSError:
                return False
        if phase == "survey":
            return (self.run_dir / "spine.json").exists()
        if phase == "plan":
            return tree_path(self.run_dir).exists()
        if phase == "pilot":
            return contract_path(self.run_dir).exists()
        return False  # research/execute/assemble: idempotent, so always re-run

    def _has_run_dir(self) -> bool:
        return self.run_dir.exists() and (self.run_dir / "events.jsonl").exists()

    def _set_phase(self, phase: str, status: str, detail: str = "") -> None:
        payload = {"phase": phase, "status": status, "detail": detail, "ts": time.time()}
        phase_path(self.run_dir).write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
        )

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

            return build_writer_adapter(
                self.options.backend,
                workspace_path=self.run_dir,
                prompt_dir=self.run_dir / "tmp" / "prompts",
                node=node,
                model=self.options.model,
            )

        return factory

    def _default_research_factory(self) -> ResearchAdapterFactory:
        def factory(node: TaskNode, query: ResearchQuery) -> AgentAdapter:
            from .backends import build_research_adapter

            return build_research_adapter(
                self.options.backend,
                workspace_path=self.run_dir,
                prompt_dir=self.run_dir / "tmp" / "prompts",
                query=query,
                model=self.options.model,
            )

        return factory


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