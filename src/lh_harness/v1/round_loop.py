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
from .gates import GateResult, all_passed, evaluate_gates
from .manifest import append_manifest_line
from .orchestrator import DispatchDecision, decide_next_action
from .provider import OpenAICompatibleProvider
from .reviewer import ReviewVerdict, review_node
from .run_dir import audit_path, events_path, manifest_path, node_artifact_path, round_trace_path
from .tree import TaskNode, TaskTree
from .writer import run_writer_node

AdapterFactory = Callable[[TaskNode], AgentAdapter]
PromptBuilder = Callable[[TaskNode], str]


async def run_round_loop(
    run_dir: str | Path,
    tree_path: str | Path,
    *,
    writer_adapter_factory: AdapterFactory,
    env: Environment,
    provider: OpenAICompatibleProvider,
    prompt_for_node: PromptBuilder,
    writer_budget: EpisodeBudget | None = None,
    max_rounds: int = 100,
    max_attempts: int = 3,
) -> TaskTree:
    run_dir = Path(run_dir)
    tree = TaskTree.load(tree_path)
    log = EventLog(events_path(run_dir))
    manifest = manifest_path(run_dir)
    budget = writer_budget or EpisodeBudget()

    async def dispatch(node: TaskNode) -> None:
        adapter = writer_adapter_factory(node)
        result, promotion = await run_writer_node(
            run_dir, node, prompt_for_node(node), adapter, env, budget
        )
        artifact_text = _read_artifact(run_dir, node.id)
        gate_results = evaluate_gates(node.gates, artifact_text)
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

    async def review(node: TaskNode) -> None:
        artifact_text = _read_artifact(run_dir, node.id)
        verdict = review_node(node, artifact_text, provider)
        _write_audit(run_dir, node, verdict)
        _transition_after_review(node, tree, tree_path, verdict, max_attempts, log)

    # Resume in-flight work before asking the orchestrator for anything new.
    for node in list(tree.nodes.values()):
        if node.status == "dispatched":
            await dispatch(node)
    for node in list(tree.nodes.values()):
        if node.status == "awaiting_review":
            await review(node)

    for round_index in range(max_rounds):
        decision = decide_next_action(tree, str(manifest), provider, round_index=round_index)
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
    else:
        node.attempts += 1
        node.status = "blocked" if node.attempts >= max_attempts else "pending"
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
    else:
        node.attempts += 1
        node.status = "blocked" if node.attempts >= max_attempts else "pending"
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


def _write_audit(run_dir: Path, node: TaskNode, verdict: ReviewVerdict) -> None:
    payload = {"node": node.id, "items": verdict.items, "verdict": verdict.verdict}
    audit_path(run_dir, node.id).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


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
    with open(round_trace_path(run_dir, round_index), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")
