"""§C5 — the eval runner: drives fixed tasks through real driver runs.

Sandbox-honest per CLAUDE.md Part III (no network, no agent binary, no API
key): the provider is scripted (a ``FakeProvider``-shaped stub that
validates every canned response against the schema it was asked for), the
Writer episodes are in-memory adapters that write an artifact directly,
and approvals are auto-resolved by the same background-thread ``Approver``
the test suite uses. Every measurement (call counts, tier, segments,
approvals, escalation events) comes out of the same disk + call-log the
real thing would produce, so an operator re-running this with a real
provider and a real gptme gets the identical report structure.

Each task runs ``runs`` times, and each run executes the driver **twice**
over the same run directory: the first ``run()`` is the fresh run, the
second is a resume against the same dir with a fresh scripted provider,
asserting zero writer dispatches (the §10 replay invariant: a resuming
process must never re-execute a completed leaf).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..pipeline import approvals as approval_store
from ..pipeline.driver import RecursiveDriver, RunOptions
from ..pipeline.run_dir import tier_path
from ..types import EpisodeResult
from ..v0.run_dir import create_run_dir
from ..v1.json_schema import validate
from ..v1.run_dir import node_artifact_path
from ..v2.survey import SpineUnit, save_spine
from ..v6.work_object import WorkObject, measure_workspace, survey_workspace
from . import measure
from .tasks import EvalTask, PASS_VERDICT, build_tasks

# A T2 review phase costs exactly three windowed document-review passes
# (v3/document_review.py's PASSES=3 over a single window, keep_depth_pass
# = False per driver._phase_review). "review" is a tier-scoped phase, so a
# T2 resume re-runs it and needs the same three responses again; T0/T1/T3
# resumes spend zero provider calls (classify/plan/pilot short-circuit on
# their durable artifacts and the execute round loop replays an
# already-passed tree without dispatching anything).
_T2_REVIEW_CALLS = 3


class _ScriptedProvider:
    """FakeProvider-shaped scripted provider (no network). Validates every
    canned response against the schema it was asked for, so a wrong canned
    response for a role fails loudly instead of silently misbehaving.
    Records every call as ``(messages, schema)`` for ``eval.measure``."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[dict[str, str]], dict[str, Any]]] = []

    def complete_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
        retries: int = 2,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((messages, schema))
        if not self._responses:
            raise AssertionError(
                f"{type(self).__name__} ran out of canned responses (call #{len(self.calls)})"
            )
        response = self._responses.pop(0)
        errors = validate(response, schema)
        if errors:
            raise AssertionError(f"canned response {response!r} does not match schema: {errors}")
        return response


class _InMemoryWriterAdapter:
    """Writes fixed content to the node's artifact path instead of
    shelling out to anything — the same pattern test_v6_tiering.py uses."""

    has_file_tools = True
    supports_session_resume = False

    def __init__(self, artifact_path: Path, content: str) -> None:
        self._artifact_path = artifact_path
        self._content = content

    async def run_episode(
        self, prompt, env, budget, live_trajectory_path=None, **kwargs
    ) -> EpisodeResult:
        self._artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self._artifact_path.write_text(self._content, encoding="utf-8")
        return EpisodeResult(status="done", actions_log="", duration_ms=1, metadata={})


def _counting_writer_factory(run_dir: Path, counter: list[int]):
    def factory(node):
        counter[0] += 1
        content = f"eval artifact body for {node.id}.\n\n{node.brief}\n"
        return _InMemoryWriterAdapter(node_artifact_path(run_dir, node.id), content)

    return factory


def _never_called_research_factory(*args, **kwargs):  # pragma: no cover
    raise AssertionError("no research/probe dispatch expected in the eval suite")


@dataclass
class RunMeasurement:
    """One task x one run: everything the aggregators in ``measure`` need,
    plus the raw facts a human reading the report wants."""

    task_id: str
    run_index: int
    tier_measured: str
    tier_final: str
    tier_override: str | None
    first_run_calls: int
    resume_calls: int
    first_run_dispatches: int
    resume_dispatches: int
    resume_ok: bool
    terminal_events: dict[str, int]
    escalations: list[dict[str, Any]]
    approvals_by_shape: dict[str, dict[str, Any]]
    mean_tokens_by_segment: dict[str, float]
    total_calls: int = field(init=False)
    calls_by_role: dict[str, int] = field(init=False)

    def __post_init__(self) -> None:
        self.total_calls = self.first_run_calls + self.resume_calls

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run": self.run_index,
            "tier_measured": self.tier_measured,
            "tier_final": self.tier_final,
            "tier_override": self.tier_override,
            "first_run_calls": self.first_run_calls,
            "resume_calls": self.resume_calls,
            "first_run_dispatches": self.first_run_dispatches,
            "resume_dispatches": self.resume_dispatches,
            "resume_ok": self.resume_ok,
            "terminal_events": self.terminal_events,
            "escalations": self.escalations,
            "approvals_by_shape": self.approvals_by_shape,
            "mean_tokens_by_segment": self.mean_tokens_by_segment,
            "total_calls": self.total_calls,
            "calls_by_role": self.calls_by_role,
        }

    def _aggregate_dict(self) -> dict[str, Any]:
        """The slice ``measure.summarize_calls_by_tier`` and
        ``measure.escalation_precision`` consume. ``total_calls`` is the
        **fresh-run** cost — the §C5 "total model calls by tier" claim is
        about what a tier's phase machinery costs to complete a task;
        the resume's re-run of tier-scoped phases is reported separately
        (``resume_calls``) as its own measurement, not folded into the
        tier's price."""
        return {
            "tier_measured": self.tier_measured,
            "tier_final": self.tier_final,
            "escalations": self.escalations,
            "total_calls": self.first_run_calls,
            "calls_by_role": self.calls_by_role,
            "resume_calls": self.resume_calls,
        }


@dataclass
class EvalReport:
    """The full §C5 report: per-run measurements plus the aggregates."""

    measurements: list[RunMeasurement]
    calls_by_tier: dict[str, Any]
    escalation_precision: dict[str, Any]
    overall_mean_tokens: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs": [m.as_dict() for m in self.measurements],
            "calls_by_tier": self.calls_by_tier,
            "escalation_precision": self.escalation_precision,
            "overall_mean_tokens_by_segment": self.overall_mean_tokens,
        }

    def print_summary(self) -> None:
        print("== calls by tier ==")
        print(json.dumps(self.calls_by_tier, indent=2))
        print("== escalation precision ==")
        print(json.dumps(self.escalation_precision, indent=2))
        print("== mean input tokens per leaf by segment ==")
        print(json.dumps(self.overall_mean_tokens, indent=2))


# ----------------------------------------------------------------------
# Fixture preparation
# ----------------------------------------------------------------------

def _prepare_task(root: Path, task: EvalTask) -> tuple[WorkObject | None, dict[str, Any] | None]:
    """Write the task's fixture into ``root`` and return the work object
    (workspace tasks) plus the canned PARTITION payload computed against
    the *exact* unit list the planner will see: the fixed corpus spine, or
    ``survey_workspace``'s own output for a workspace (pure code, same
    input the driver's ``_phase_explore`` uses, so the partition tiles it
    exactly)."""
    create_run_dir(root, "run")
    run_dir = root / "run"
    work_object: WorkObject | None = None
    plan_payload: dict[str, Any] | None = None
    if task.workspace:
        work_root = root / "work"
        for rel, content in task.workspace.items():
            path = work_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        work_object = measure_workspace(work_root)
        if task.plan is not None:
            units = survey_workspace(work_object)
            plan_payload = {"children": task.plan(units)}
    else:
        units = [SpineUnit(*u) for u in task.spine_units]
        if units:
            save_spine(run_dir, units)
        if task.plan is not None:
            plan_payload = {"children": task.plan(units)}
    return work_object, plan_payload


def _options(task: EvalTask, work_object: WorkObject | None) -> RunOptions:
    kwargs: dict[str, Any] = dict(
        goal=task.goal,
        # document_order skips the round loop's per-round orchestrator
        # model call (PLAN-zeromem.md §1) — the eval suite measures the
        # tier/phase machinery, not orchestrator prompt-tuning.
        dispatch_policy="document_order",
    )
    if task.corpus:
        kwargs["source_text"] = task.corpus
    if work_object is not None:
        kwargs["work_object"] = work_object
    return RunOptions(**kwargs)


def _canned_responses(
    task: EvalTask, plan_payload: dict[str, Any] | None, *, resume: bool
) -> list[dict[str, Any]]:
    """The scripted responses one driver.run() of this task needs, in call
    order. Fresh: ESTIMATE (classify) → PARTITION (plan, T2/T3) → the T2
    review phase's three windowed verdict calls. Resume: only the T2
    review re-run; T0/T1/T3 resume with no provider traffic at all."""
    if resume:
        if task.expected_tier == "T2":
            return [PASS_VERDICT] * _T2_REVIEW_CALLS
        return []
    responses: list[dict[str, Any]] = [task.estimate]
    if plan_payload is not None:
        responses.append(plan_payload)
    if task.expected_tier == "T2":
        responses.extend([PASS_VERDICT] * _T2_REVIEW_CALLS)
    return responses


async def _run_driver(
    run_dir: Path,
    task: EvalTask,
    work_object: WorkObject | None,
    plan_payload: dict[str, Any] | None,
    responses: list[dict[str, Any]],
    dispatches: list[int],
) -> _ScriptedProvider:
    provider = _ScriptedProvider(responses)
    driver = RecursiveDriver(
        run_dir,
        provider=provider,  # type: ignore[arg-type]
        options=_options(task, work_object),
        writer_adapter_factory=_counting_writer_factory(run_dir, dispatches),
        research_adapter_factory=_never_called_research_factory,
        probe_adapter_factory=_never_called_research_factory,
        poll_interval=0.02,
    )
    with approval_store.Approver(run_dir, poll_interval=0.02):
        report = await driver.run()
    if report.status != "done":
        raise AssertionError(
            f"task {task.task_id}: driver finished {report.status!r}, expected 'done'"
        )
    return provider


def _read_tier(run_dir: Path) -> tuple[str, str, str | None]:
    record = json.loads(tier_path(run_dir).read_text(encoding="utf-8"))
    return (
        str(record.get("tier", "")),
        str(record.get("measured_tier", record.get("tier", ""))),
        record.get("override"),
    )


async def _run_task_once(root: Path, task: EvalTask, run_index: int) -> RunMeasurement:
    run_dir = root / "run"
    work_object, plan_payload = _prepare_task(root, task)

    # --- fresh run -------------------------------------------------
    dispatches: list[int] = [0]
    provider = await _run_driver(
        run_dir,
        task,
        work_object,
        plan_payload,
        _canned_responses(task, plan_payload, resume=False),
        dispatches,
    )
    tier_final, tier_measured, tier_override = _read_tier(run_dir)
    first_run_calls = len(provider.calls)
    first_run_dispatches = dispatches[0]

    # --- resume over the same directory -----------------------------
    resume_dispatches: list[int] = [0]
    provider2 = await _run_driver(
        run_dir,
        task,
        work_object,
        plan_payload,
        _canned_responses(task, plan_payload, resume=True),
        resume_dispatches,
    )
    resume_dispatch_count = resume_dispatches[0]
    resume_calls = len(provider2.calls)

    m = RunMeasurement(
        task_id=task.task_id,
        run_index=run_index,
        tier_measured=tier_measured,
        tier_final=tier_final,
        tier_override=tier_override,
        first_run_calls=first_run_calls,
        resume_calls=resume_calls,
        first_run_dispatches=first_run_dispatches,
        resume_dispatches=resume_dispatch_count,
        resume_ok=resume_dispatch_count == 0,
        terminal_events=measure.terminal_events_per_node(run_dir),
        escalations=measure.escalation_events(run_dir),
        approvals_by_shape=measure.approval_rate_by_shape(run_dir),
        mean_tokens_by_segment=measure.mean_tokens_by_segment(run_dir),
    )
    m.calls_by_role = measure.calls_by_role(provider.calls)
    return m


async def run_eval_suite(
    tasks: tuple[EvalTask, ...] | None = None,
    *,
    runs: int = 3,
    report_path: Path | None = None,
) -> EvalReport:
    """Drive ``runs`` copies of every task through real driver runs and
    aggregate the measurements. With ``report_path`` given, writes the
    JSON report there; ``print_summary`` on the returned report prints the
    aggregates to the terminal."""
    tasks = tasks or build_tasks()
    measurements: list[RunMeasurement] = []
    for task in tasks:
        for run_index in range(runs):
            with tempfile.TemporaryDirectory() as root_str:
                measurements.append(await _run_task_once(Path(root_str), task, run_index))

    aggregate_dicts = [m._aggregate_dict() for m in measurements]
    report = EvalReport(
        measurements=measurements,
        calls_by_tier=measure.summarize_calls_by_tier(aggregate_dicts),
        escalation_precision=measure.escalation_precision(aggregate_dicts),
        overall_mean_tokens=_mean_segments(measurements),
    )
    if report_path is not None:
        report_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return report


def _mean_segments(measurements: list[RunMeasurement]) -> dict[str, float]:
    """Column means over every run's per-leaf segment means. Labels absent
    from a run are not counted as zeros (see ``measure.mean_tokens_by_
    segment`` for the reasoning)."""
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for m in measurements:
        for label, tokens in m.mean_tokens_by_segment.items():
            totals[label] = totals.get(label, 0.0) + tokens
            counts[label] = counts.get(label, 0) + 1
    return {label: round(totals[label] / counts[label], 1) for label in totals}


def run_eval_suite_sync(
    tasks: tuple[EvalTask, ...] | None = None,
    *,
    runs: int = 3,
    report_path: Path | None = None,
) -> EvalReport:
    """Synchronous entry point (``unittest`` tests and the CLI both use
    it) wrapping the async suite."""
    return asyncio.run(run_eval_suite(tasks=tasks, runs=runs, report_path=report_path))
