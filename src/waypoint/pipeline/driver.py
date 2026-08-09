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

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..adapters.base import AgentAdapter
from ..environment.base import Environment
from ..types import EpisodeBudget
from ..v0.events import EventLog
from ..v0.run_dir import create_run_dir, events_path, manifest_path, node_artifact_path, spec_path
from ..v1.provider import OpenAICompatibleProvider
from ..v1.reviewer import ReviewVerdict
from ..v1.round_loop import run_round_loop
from ..v1.tree import TaskNode, TaskTree
from ..v2.contract import ContractRule, freeze_contract
from ..v2.intake import run_intake
from ..v2.pilot import approve_pilot, run_pilot, select_pilot_nodes
from ..v2.planner import build_tree
from ..v2.run_dir import contract_path
from ..v2.survey import SpineUnit, assemble_spine, chunk_text, load_spine, save_spine, survey_chunks
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

    def to_spec(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "backend": self.backend,
            "model": self.model,
            "source_text": self.source_text,
            "compile_command": self.compile_command,
            "research_plan": [
                {"node_id": node_id, **asdict(query)}
                for node_id, queries in self.research_plan.items()
                for query in queries
            ],
            "max_rounds": self.max_rounds,
            "max_attempts": self.max_attempts,
        }

    @staticmethod
    def from_spec(data: dict[str, Any]) -> "RunOptions":
        return RunOptions(
            goal=str(data.get("goal", "")),
            backend=str(data.get("backend", "gptme")),
            model=data.get("model"),
            source_text=str(data.get("source_text", "")),
            compile_command=data.get("compile_command"),
            research_plan=parse_research_plan(data.get("research_plan")),
            max_rounds=int(data.get("max_rounds", 100)),
            max_attempts=int(data.get("max_attempts", 3)),
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
        self.log = EventLog(events_path(self.run_dir))

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------
    async def run(self) -> RunReport:
        create_run_dir(self.run_dir.parent, self.run_dir.name)
        self._write_source_and_spec()
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
        self._log({"node_id": "-", "role": "harness", "round": 0, "type": "run_completed"})
        return report

    async def _run_phase(self, phase: str, *, round_index: int) -> RunReport:
        if self._phase_done(phase):
            return RunReport(status="done", phase=phase)
        self._set_phase(phase, _IN_PROGRESS)
        self._log({"node_id": "-", "role": "harness", "round": round_index, "type": "phase_started", "phase": phase})
        try:
            outcome: Any = await getattr(self, f"_phase_{phase}")()
        except Exception as exc:  # noqa: BLE001 — the phase boundary is the reporter
            self._set_phase(phase, "error", detail=str(exc))
            self._log(
                {"node_id": "-", "role": "harness", "round": round_index, "type": "phase_failed", "phase": phase}
            )
            return RunReport(status="error", phase=phase, detail=str(exc))
        status = "done" if outcome is not False else "escalated"
        self._set_phase(phase, status)
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

    def _answer_intake(self, question: str) -> str:
        approval = self._ask(
            "intake_question",
            title="Intake question",
            message=question,
            input_label="Your answer",
        )
        return approval.user_input.strip()

    async def _phase_survey(self) -> None:
        source = source_path(self.run_dir).read_text(encoding="utf-8").strip()
        if not source:
            units = [SpineUnit(id="unit-01", label="The goal", start_chunk=0, end_chunk=0, tokens=0)]
        else:
            chunks = chunk_text(source)
            votes = survey_chunks(chunks, self.provider)
            units = assemble_spine(chunks, votes)
        save_spine(self.run_dir, units)

    async def _phase_plan(self) -> None:
        tree = build_tree(load_spine(self.run_dir), self.provider)
        tree.save(tree_path(self.run_dir))

    async def _phase_pilot(self) -> None:
        tree = self._load_tree()
        rules: list[ContractRule] = []
        budget = EpisodeBudget()
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
                    EpisodeBudget(),
                    self.log,
                )
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
            prompt_for_node=lambda node: build_node_prompt(node, self.run_dir),
            writer_budget=EpisodeBudget(),
            max_rounds=self.options.max_rounds,
            max_attempts=self.options.max_attempts,
        )
        tree = self._load_tree()
        return None if not tree.is_blocked() else False

    async def _phase_assemble(self) -> None:
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
        return approval_store.wait_for_resolution(
            self.run_dir, approval.approval_id, poll_interval=self.poll_interval
        )

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

    def _has_plan_run_dir(self) -> bool:
        return (self.run_dir / "events.jsonl").exists()

    def _set_phase(self, phase: str, status: str, detail: str = "") -> None:
        payload = {"phase": phase, "status": status, "detail": detail, "ts": time.time()}
        phase_path(self.run_dir).write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
        )

    def _halted(self) -> bool:
        return halt_path(self.run_dir).exists()

    def _load_tree(self) -> TaskTree:
        try:
            return TaskTree.load(tree_path(self.run_dir))
        except (OSError, ValueError):
            return TaskTree(nodes={})

    def _log(self, event: dict[str, Any]) -> None:
        self.log.append(event)

    def _write_source_and_spec(self) -> None:
        source_path(self.run_dir).write_text(self.options.source_text, encoding="utf-8")
        spec = run_spec_path(self.run_dir)
        if not spec.exists():
            spec.write_text(json.dumps(self.options.to_spec(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

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


def _count_statuses(tree: TaskTree) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in tree.nodes.values():
        counts[node.status] = counts.get(node.status, 0) + 1
    return counts


# ----------------------------------------------------------------------
# Post-run interventions (PLAN.md §10): contract amendment -> re-validation
# triage, then apply; reopen one passed node as a scoped repair.
# ----------------------------------------------------------------------
async def amend_and_revalidate(
    run_dir: str | Path,
    *,
    rule_text: str,
    reason: str,
    provider: OpenAICompatibleProvider,
) -> dict[str, Any]:
    """§10 contract amendment, first half: append the rule, run the
    read-only re-validation pass, and return ``{contract, counts, triage}``
    for the operator to review before any repair is dispatched (§10:
    "Present counts, get approval, then execute"). No writer runs here."""
    from ..v2.contract import amend_contract
    from ..v3.revalidate import summarize_triage

    run_dir = Path(run_dir)
    contract_text = amend_contract(run_dir, rule_text, reason=reason)
    tree = TaskTree.load(tree_path(run_dir))
    triage_by_node = run_revalidation_pass(
        run_dir, tree, tree_path(run_dir), contract_text, provider
    )
    return {
        "contract": contract_text,
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