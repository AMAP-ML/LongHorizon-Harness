"""The v3 assembly entrypoint (PLAN.md §4.6, §13): concatenate, run
cross-cutting checks, compile, and repair compile failures — mirroring how
v1's ``round_loop.py`` is the entrypoint that ties Orchestrator/Writer/
Reviewer together, this is the entrypoint that ties assemble/checks/compile/
repair together.

Order matches §4.6's own ordering (concatenation, then checks, then
compile) because each step is a cheaper, more targeted gate than the next:
checks are free and catch structural breakage (a missing artifact) that
would otherwise surface as a confusing compile error; compiling a doc that
already fails checks just wastes the compile budget.

**Compile-failure repair loop** (§4.6.3: "A compile error becomes a repair
node scoped to the offending file, which goes back through review"): this
module never edits ``out/`` itself — see repair.py's docstring for why that
guardrail matters. It only ever identifies *which* passed node's artifact
the compile log implicates (``find_offending_nodes``, a plain substring
match against each node's artifact filename — this harness has no
LaTeX-log parser and doesn't need one to attribute a failure to a file) and
hands that off to ``repair.run_repair``. If the log can't be attributed to
any node, this stops and escalates rather than guessing (same "don't loop
forever, don't guess" posture as v1's ``max_attempts`` and PLAN.md §10's
"if the amendment was itself the mistake, you'll only realize it three
chapters in" — better to ask than to thrash).

**Document-level review** (PLAN-zeromem.md §8, opt-in via
``document_review=True``): after assemble and before compile, run the
read-only document review passes; the triage rides back on
``AssemblyRunResult.review`` so the caller can present counts through an
approval and dispatch repairs via the existing §10 path — repairs are
never dispatched from inside this module. An unattributable review defect
escalates exactly like an unattributable compile failure does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..adapters.base import AgentAdapter
from ..environment.base import Environment
from ..types import EpisodeBudget
from ..v0.events import EventLog
from ..v0.run_dir import events_path
from ..v1.provider import OpenAICompatibleProvider
from ..v1.tree import TaskNode, TaskTree
from .assemble import AssemblyNotReadyError, AssemblyOutput, assemble
from .checks import CheckResult, run_cross_cutting_checks, write_checks_json
from .compile import CompileResult, run_compile
from .document_review import DocumentReviewResult, run_document_review
from .repair import RepairOutcome, run_repair

AdapterFactory = Callable[[TaskNode], AgentAdapter]

_LOG_EXCERPT_CHARS = 2000


@dataclass
class AssemblyRunResult:
    assembly: AssemblyOutput | None
    checks: list[CheckResult]
    compile_result: CompileResult | None
    repairs: list[RepairOutcome] = field(default_factory=list)
    escalated: bool = False
    escalation_reason: str | None = None
    review: DocumentReviewResult | None = None


def find_offending_nodes(tree: TaskTree, log_text: str) -> list[str]:
    """Which passed nodes' artifacts the compile log mentions by filename,
    in tree (document) order. A plain substring match, not a format-specific
    log parser — good enough to scope a repair, and honest about not
    guessing when nothing matches."""
    matches = []
    for node in tree.nodes.values():
        if node.status != "passed":
            continue
        filename = Path(node.artifact).name
        if filename and filename in log_text:
            matches.append(node.id)
    return matches


async def run_assembly_loop(
    run_dir: str | Path,
    tree_path: str | Path,
    manifest_path: str | Path,
    *,
    writer_adapter_factory: AdapterFactory,
    env: Environment,
    provider: OpenAICompatibleProvider,
    compile_command: str | None = None,
    writer_budget: EpisodeBudget | None = None,
    max_repairs: int = 3,
    max_attempts: int = 3,
    filename: str = "main.md",
    document_review: bool = False,
) -> AssemblyRunResult:
    run_dir = Path(run_dir)
    tree = TaskTree.load(tree_path)
    log = EventLog(events_path(run_dir))
    budget = writer_budget or EpisodeBudget()

    checks = run_cross_cutting_checks(run_dir, tree, manifest_path)
    write_checks_json(run_dir, checks)
    if not all(c.passed for c in checks):
        return _escalate(log, checks, "cross-cutting checks failed before assembly")

    try:
        assembly = assemble(run_dir, tree, filename=filename)
    except AssemblyNotReadyError as exc:
        return _escalate(log, checks, str(exc))

    review: DocumentReviewResult | None = None
    if document_review:
        review = run_document_review(run_dir, tree, provider, log=log)
        if review.escalated:
            return AssemblyRunResult(
                assembly=assembly,
                checks=checks,
                compile_result=None,
                escalated=True,
                escalation_reason=f"document review: {review.escalation_reason}",
                review=review,
            )

    compile_result = await run_compile(run_dir, env, compile_command)
    repairs: list[RepairOutcome] = []
    repair_round = 0

    while not compile_result.passed and repair_round < max_repairs:
        repair_round += 1
        offending = find_offending_nodes(tree, compile_result.log)
        if not offending:
            return AssemblyRunResult(
                assembly=assembly,
                checks=checks,
                compile_result=compile_result,
                repairs=repairs,
                escalated=True,
                escalation_reason="compile failed and the log could not be attributed to any node",
            )

        defect = f"Compile failed. Log excerpt:\n{compile_result.log[:_LOG_EXCERPT_CHARS]}"
        for node_id in offending:
            node = tree.nodes[node_id]
            adapter = writer_adapter_factory(node)
            outcome = await run_repair(
                run_dir, node, tree, tree_path, manifest_path, defect,
                adapter, env, budget, provider, log,
                mode="patch", max_attempts=max_attempts,
            )
            repairs.append(outcome)

        # §11.7: a repair that left the node stale/blocked makes assemble()
        # raise — that must escalate like every other not-ready state, not
        # escape the loop as an uncaught AssemblyNotReadyError.
        try:
            assembly = assemble(run_dir, tree, filename=filename)
        except AssemblyNotReadyError as exc:
            return _escalate(log, checks, str(exc))
        compile_result = await run_compile(run_dir, env, compile_command)

    escalated = not compile_result.passed
    reason = "compile still failing after exhausting repair attempts" if escalated else None
    if escalated:
        log.append({"node_id": "-", "role": "harness", "round": 0, "type": "run_escalated", "reason": reason})

    return AssemblyRunResult(
        assembly=assembly,
        checks=checks,
        compile_result=compile_result,
        repairs=repairs,
        escalated=escalated,
        escalation_reason=reason,
        review=review,
    )


def _escalate(log: EventLog, checks: list[CheckResult], reason: str) -> AssemblyRunResult:
    log.append({"node_id": "-", "role": "harness", "round": 0, "type": "run_escalated", "reason": reason})
    return AssemblyRunResult(
        assembly=None, checks=checks, compile_result=None, escalated=True, escalation_reason=reason
    )
