"""The v1 round loop (PLAN.md §13): Orchestrator/Writer/Reviewer driven one
round at a time, task state kept entirely in ``tree.json``.

Resumability (§10) composes with v0 rather than reimplementing it:

- A node already "passed" is never revisited — ``TaskTree.is_ready`` only
  offers "pending" nodes to the orchestrator.
- A node caught mid-flight by a crash (its ``tree.json`` status is still
  "dispatched" or "awaiting_review" when this process starts) is resumed
  directly, *before* the orchestrator is asked anything this round —
  dispatch decisions are for new work, not for continuing what was already
  underway. The in-flight writer continuation itself is v0's ``run_node``,
  completely unmodified: it replays from ``events.jsonl`` exactly as it
  does for a single-node run.

Invariant enforced here, not by either role (PLAN.md §2 invariant 1): a
node's status only ever becomes "passed" after both its gates (code) and
its reviewer verdict (model, only consulted when the node declares
judgment items) agree.

``_transition_after_writer``/``_transition_after_review`` also record the
located gate or reviewer failure onto ``node.last_defect`` on every failed
attempt, and clear it on success (PLAN-zeromem.md §9) — a retry's prompt
(``pipeline/prompts.py:build_node_prompt``) reads it back, so attempts 2
and 3 carry forward what attempt 1 got wrong instead of resampling an
identical prompt blind.

``dispatch_node``/``review_and_transition_node`` (PLAN.md §B2) are
``run_round_loop``'s per-node dispatch/review bodies, pulled out of their
original inline closures into standalone functions — the *only* change
this made to this module's own behavior is that they're callable directly.
This exists so ``v6/direct.py``'s T0 direct-episode path (one gated
episode, no ``tree.json``) can reuse the exact same Writer-dispatch and
Reviewer-verdict machinery instead of re-implementing it: T0 hands both
functions an in-memory, single-node ``TaskTree`` and a JSON path of its own
choosing (never named ``tree.json`` — PLAN.md §A4.3: "T0 has no
``tree.json``"), and gets the identical gate-cache-write, manifest-line,
and status-transition behavior a real round-loop dispatch gets.

**Runtime split (PLAN.md §A8/§B5) is wired here as two optional, injected
callables — ``split_handler`` and ``on_node_passed`` — never as a direct
import of ``v7/split.py``.** This package's layering is strictly
bottom-up (``v0`` -> ``v1`` -> ``v2`` -> ...; grep any ``v1/*.py`` and none
of them import from ``v2`` or later), and ``v7/split.py`` needs ``TaskTree``/
``TaskNode`` from here plus ``leaf_gate`` from ``v2/planner.py`` — so this
module cannot import it without inverting that rule. Instead
``dispatch_node`` and ``review_and_transition_node`` accept the real
functions as parameters, and ``pipeline/driver.py`` (which already imports
both ``v1`` and ``v7``) is the one place that wires
``v7.split.handle_split_proposal``/``v7.split.maybe_derive_split_parent``
in — and only for the T2/T3 tiers, never for T0/T1 (``v6/direct.py``'s own
calls to these two functions never pass either callable, so its behavior
is byte-for-byte unchanged: PLAN.md §A8.1's mechanism only makes sense
against a real, multi-node-capable ``tree.json``, which T0 has none of and
T1's is a single node the driver builds directly with its own, different,
already-built escalation path — ``v6/direct.py:is_size_defect`` /
``size_defect_retry``). Left ``None`` (every existing caller of these three
functions, and every test of this module before §B5), ``split_handler``/
``on_node_passed`` cost nothing and change nothing: split detection is
purely additive.

``split_handler(run_dir, node, tree, tree_path, log) -> bool`` runs right
after the Writer episode returns, before gate evaluation — a split
proposal is a third kind of episode termination (submit / split / fail,
§A8.1), and gate-evaluating a blank ``out/<node>.md`` from a node that
proposed a split instead of submitting would misread it as a plain
"fail" and burn an attempt PLAN.md explicitly says a rejected proposal
must not cost. Returning ``True`` means a ``split.json`` was present and
fully handled (accepted-and-grafted, or evaluated-and-rejected) — either
way ``dispatch_node`` returns immediately without touching gates, the
manifest, or ``_transition_after_writer``. Returning ``False`` means no
``split.json`` existed at all, and ``dispatch_node`` proceeds exactly as
before this workstream.

``on_node_passed(run_dir, node, tree, tree_path, log) -> None`` runs
inside ``review_and_transition_node`` immediately after a node's status
becomes "passed" — the natural place to ask "did this completion just
finish off a split parent's last outstanding child," mirroring how
dependency-based readiness (``TaskTree.is_ready``) is already re-checked
reactively rather than polled. Re-checking on every passing node (instead
of tracking "is this the last one" explicitly) is deliberately the simple
option: the condition itself (``all(sibling.status == "passed" for
sibling in siblings)``) is cheap and idempotent to recompute, so there is
nothing to get out of sync.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from ..adapters.base import AgentAdapter
from ..environment.base import Environment
from ..types import EpisodeBudget
from ..v0.events import EventLog
from ..v0.run_dir import write_text_atomic
from .gates import GateResult, all_passed, evaluate_gates, unmet, write_gate_cache
from .manifest import append_manifest_line
from .orchestrator import (
    DispatchDecision,
    DispatchPolicy,
    decide_next_action_with_policy,
)
from .provider import OpenAICompatibleProvider
from .reviewer import ReviewVerdict, review_node
from .run_dir import (
    ensure_audit_path,
    ensure_orchestrator_dir,
    events_path,
    manifest_path,
    node_artifact_path,
    orchestrator_dir,
)
from .tree import TaskNode, TaskTree
from .writer import run_writer_node

AdapterFactory = Callable[[TaskNode], AgentAdapter]
PromptBuilder = Callable[[TaskNode], str]
# PLAN.md §A8/§B5 — see module docstring for why these are injected
# callables rather than a direct import of v7/split.py.
SplitHandler = Callable[[Path, TaskNode, TaskTree, Path, EventLog], bool]
NodePassedHook = Callable[[Path, TaskNode, TaskTree, Path, EventLog], None]


async def dispatch_node(
    run_dir: str | Path,
    node: TaskNode,
    tree: TaskTree,
    tree_path: str | Path,
    *,
    writer_adapter_factory: AdapterFactory,
    env: Environment,
    prompt_for_node: PromptBuilder,
    budget: EpisodeBudget,
    manifest: str | Path,
    max_attempts: int,
    log: EventLog,
    split_handler: SplitHandler | None = None,
) -> None:
    """One Writer episode + gate evaluation + manifest line + status
    transition for a single node — ``run_round_loop``'s original ``dispatch``
    closure, pulled out so ``v6/direct.py``'s T0 path can call it directly
    against its own in-memory single-node ``TaskTree`` (module docstring)."""
    run_dir = Path(run_dir)
    adapter = writer_adapter_factory(node)
    result, promotion = await run_writer_node(
        run_dir, node, prompt_for_node(node), adapter, env, budget
    )
    if split_handler is not None and split_handler(run_dir, node, tree, tree_path, log):
        # A split.json existed and was fully evaluated (accepted-and-grafted
        # or rejected-with-attempt-preserved) — module docstring. Either way
        # this was not a "submit", so gates/manifest/the normal
        # writer-failure transition never apply.
        return
    artifact_text = _read_artifact(run_dir, node.id)
    gate_results = evaluate_gates(node.gates, artifact_text)
    # §11.10.11: evaluated once per dispatch (deterministic), durable in
    # the audit file for every consumer; review and the dashboard read
    # this instead of re-evaluating.
    audit = ensure_audit_path(run_dir, node.id)
    write_gate_cache(audit, gate_results)
    append_manifest_line(
        manifest,
        node_id=node.id,
        artifact_path=str(node_artifact_path(run_dir, node.id)),
        artifact_text=artifact_text,
        gate_results=gate_results,
        promotion=promotion,
    )
    _transition_after_writer(
        node, tree, tree_path, result.status == "done", gate_results, max_attempts, log
    )


async def review_and_transition_node(
    run_dir: str | Path,
    node: TaskNode,
    tree: TaskTree,
    tree_path: str | Path,
    *,
    provider: OpenAICompatibleProvider,
    max_attempts: int,
    log: EventLog,
    on_node_passed: NodePassedHook | None = None,
) -> None:
    """One Reviewer verdict + status transition for a single node —
    ``run_round_loop``'s original ``review`` closure, pulled out for the
    same reason as ``dispatch_node`` above."""
    run_dir = Path(run_dir)
    artifact_text = _read_artifact(run_dir, node.id)
    verdict = review_node(node, artifact_text, provider)
    _write_audit(run_dir, node, verdict)
    _transition_after_review(node, tree, tree_path, verdict, max_attempts, log)
    if node.status == "passed" and on_node_passed is not None:
        # PLAN.md §A8.3: this node may be the last outstanding child of a
        # split parent — see module docstring for why this is a cheap
        # recheck rather than tracked state.
        on_node_passed(run_dir, node, tree, tree_path, log)


async def run_round_loop(
    run_dir: str | Path,
    tree_path: str | Path,
    *,
    writer_adapter_factory: AdapterFactory,
    env: Environment,
    provider: OpenAICompatibleProvider,
    prompt_for_node: PromptBuilder,
    writer_budget: EpisodeBudget | None = None,
    writer_budget_for: Callable[[TaskNode], EpisodeBudget] | None = None,
    max_rounds: int = 100,
    max_attempts: int = 3,
    dispatch_policy: DispatchPolicy = "model",
    split_handler: SplitHandler | None = None,
    on_node_passed: NodePassedHook | None = None,
) -> TaskTree:
    run_dir = Path(run_dir)
    tree = TaskTree.load(tree_path)
    log = EventLog(events_path(run_dir))
    manifest = manifest_path(run_dir)
    default_budget = writer_budget or EpisodeBudget()

    async def dispatch(node: TaskNode) -> None:
        budget = writer_budget_for(node) if writer_budget_for is not None else default_budget
        await dispatch_node(
            run_dir,
            node,
            tree,
            tree_path,
            writer_adapter_factory=writer_adapter_factory,
            env=env,
            prompt_for_node=prompt_for_node,
            budget=budget,
            manifest=manifest,
            max_attempts=max_attempts,
            log=log,
            split_handler=split_handler,
        )

    async def review(node: TaskNode) -> None:
        await review_and_transition_node(
            run_dir,
            node,
            tree,
            tree_path,
            provider=provider,
            max_attempts=max_attempts,
            log=log,
            on_node_passed=on_node_passed,
        )

    # Resume in-flight work before asking the orchestrator for anything new.
    for node in list(tree.nodes.values()):
        if node.status == "dispatched":
            await dispatch(node)
    for node in list(tree.nodes.values()):
        if node.status == "awaiting_review":
            await review(node)

    # §11.10.16: round indices continue across process runs. round_index
    # used to restart at 0 on every resume while the trace files were opened
    # "a", so round 0 of the third resume appended to round 0 of the first —
    # one file, three processes' interleaved rounds, impossible to separate.
    # The orchestrator's own view is stateless per round, so rebasing the
    # numbers is free: this run's rounds are just pick up where the last
    # process left off.
    first_round = _next_round_index(run_dir)
    for offset in range(max_rounds):
        round_index = first_round + offset
        decision = decide_next_action_with_policy(
            tree,
            str(manifest),
            provider,
            round_index=round_index,
            policy=dispatch_policy,
        )
        _write_round_trace(run_dir, round_index, tree, decision)

        if decision.action == "halt":
            break
        if decision.action == "escalate":
            log.append(
                {
                    "node_id": decision.node_id or "-",
                    "role": "orchestrator",
                    "round": round_index,
                    "type": "run_escalated",
                    "reason": decision.reason,
                }
            )
            break

        node = tree.nodes[decision.node_id]
        node.status = "dispatched"
        tree.save(tree_path)
        log.append(
            {
                "node_id": node.id,
                "role": "orchestrator",
                "round": round_index,
                "type": "node_dispatch_decided",
                "reason": decision.reason,
            }
        )
        await dispatch(node)
        if node.status == "awaiting_review":
            await review(node)
        # §11.10.5: a gate or review failure that still has attempts left is
        # a retry of the node the harness already knows it wants. Re-dispatch
        # in place instead of round-tripping the orchestrator for a call
        # whose only possible answer is "dispatch the same node again" — the
        # retry's prompt differs (last_defect is carried forward by
        # PLAN-zeromem.md §9), so this is a correction, not a resample.
        while node.status == "pending" and node.attempts < max_attempts:
            node.status = "dispatched"
            tree.save(tree_path)
            await dispatch(node)
            if node.status == "awaiting_review":
                await review(node)

    return tree


def _read_artifact(run_dir: Path, node_id: str) -> str:
    path = node_artifact_path(run_dir, node_id)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _transition_after_writer(
    node: TaskNode,
    tree: TaskTree,
    tree_path: str | Path,
    episode_ok: bool,
    gate_results: list[GateResult],
    max_attempts: int,
    log: EventLog,
) -> None:
    if episode_ok and all_passed(gate_results):
        node.status = "awaiting_review"
        node.last_defect = ""
    else:
        node.attempts += 1
        node.status = "blocked" if node.attempts >= max_attempts else "pending"
        # PLAN-zeromem.md §9: carry the located failure forward so a retry's
        # prompt differs from the first attempt's instead of resampling the
        # same instructions blind.
        node.last_defect = "; ".join(
            f"{result.gate}: {result.detail}" for result in unmet(gate_results)
        ) or "episode did not complete"
        log.append(
            {
                "node_id": node.id,
                "role": "harness",
                "round": 0,
                "type": "node_gate_failed",
                "attempts": node.attempts,
                "episode_ok": episode_ok,
                "unmet": [result.gate for result in gate_results if not result.passed],
            }
        )
    tree.save(tree_path)


def _transition_after_review(
    node: TaskNode,
    tree: TaskTree,
    tree_path: str | Path,
    verdict: ReviewVerdict,
    max_attempts: int,
    log: EventLog,
) -> None:
    if verdict.verdict == "pass":
        # PLAN.md invariant 1: only the harness writes "passed", and only
        # after gates (checked in _transition_after_writer) and review
        # (checked here) both agree.
        node.status = "passed"
        node.last_defect = ""
    else:
        node.attempts += 1
        node.status = "blocked" if node.attempts >= max_attempts else "pending"
        node.last_defect = _defect_from_verdict(verdict)
        log.append(
            {
                "node_id": node.id,
                "role": "harness",
                "round": 0,
                "type": "node_review_failed",
                "attempts": node.attempts,
            }
        )
    tree.save(tree_path)


def _defect_from_verdict(verdict: ReviewVerdict) -> str:
    """Located, scoped feedback (PLAN-zeromem.md §9) rather than a bare
    "fail" — the same join v3/revalidate.py already does for repair
    prompts, applied one layer earlier so an ordinary retry gets it too."""
    lines = [
        f"{item.get('id', '?')}: {item.get('defect', '')}".rstrip(": ")
        for item in verdict.items
        if not item.get("pass", True)
    ]
    return "\n".join(lines) if lines else "reviewer verdict: fail"


def _write_audit(run_dir: Path, node: TaskNode, verdict: ReviewVerdict) -> None:
    path = ensure_audit_path(run_dir, node.id)
    gates: dict | None = None
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                gates = loaded
            elif isinstance(loaded, list):
                # §11.10.11: the dispatch-time gate cache is the bare list
                # write_gate_cache produced; carry it under "gates".
                gates = {"gates": loaded}
        except ValueError:
            gates = None
    existing = gates or {}
    # §11.10.11: merge, never replace — the gate cache written at dispatch
    # must survive the reviewer's write of items/verdict.
    existing.update(
        {
            "node": node.id,
            "items": verdict.items,
            "verdict": verdict.verdict,
            "truncated": verdict.truncated,
        }
    )
    path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _next_round_index(run_dir: Path) -> int:
    """First round number for this process run: one past the highest
    ``round-NNN.jsonl`` already on disk. §11.10.16 — see the caller."""
    trace_dir = orchestrator_dir(run_dir)
    if not trace_dir.exists():
        return 0
    highest = -1
    for path in trace_dir.glob("round-*.jsonl"):
        stem = path.stem
        try:
            index = int(stem.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        highest = max(highest, index)
    return highest + 1


def _write_round_trace(
    run_dir: Path, round_index: int, tree: TaskTree, decision: DispatchDecision
) -> None:
    line = {
        "round": round_index,
        "ts": time.time(),
        "ready_nodes": tree.ready_nodes(),
        "decision": {
            "action": decision.action,
            "node_id": decision.node_id,
            "reason": decision.reason,
        },
    }
    with open(ensure_orchestrator_dir(run_dir) / f"round-{round_index:03d}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")
