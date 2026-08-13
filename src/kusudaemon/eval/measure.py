"""PLAN.md §C5 measurement functions — pure analysis over a run directory
plus a recorded provider-call list.

The eval harness's job is a cost claim plus a correctness claim, both of
which must come out of the run directory and the call log rather than out
of prose: **calls-by-tier** ("the entire claim of §A4 is a cost claim, and
a cost claim without a number is a preference") and **escalation
precision** ("high escalation means the classifier is too aggressive;
zero escalation across varied tasks means it is too conservative").

Every function here is deterministic and disk-based — no provider calls,
no network. The harness drives real ``RecursiveDriver`` runs (fake
provider/adapters in the sandbox, real ones when an operator runs it with
a key) and hands the recorded calls to these functions; the same
functions serve both, so the number a fake run reports is the number a
real run reports.

Provider calls are recorded as ``(messages, schema)`` pairs (the exact
shape ``FakeProvider.calls`` keeps). Calls are classified by their
schema's top-level properties — each ``complete_json`` role in this
codebase asks for a structurally distinct schema, so the classification is
stable without touching the driver. ``VERDICT_SCHEMA`` serves both the
per-leaf reviewer and document review's windowed passes; both are
"reviewer" calls here, which is the honest accounting for the §C5 metric
(every verdict call is part of the review tier's cost).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from ..pipeline import approvals as approval_store
from ..pipeline.prompts import build_node_prompt
from ..v1.gates import estimate_tokens
from ..v1.tree import TaskNode
from ..v6.direct import DIRECT_NODE_ID, direct_node_path
from ..v1.run_dir import tree_path

# Role classification, most-specific first: the order matters only where
# two schemas share a property name, which none of the required sets do.
_ROLE_BY_PROPS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "classify",
        frozenset({"files_touched", "artifacts", "answerable_without_exploration"}),
    ),
    ("intake", frozenset({"questions", "objections"})),
    ("planner", frozenset({"children"})),
    ("probe_planner", frozenset({"probes"})),
    ("reviewer", frozenset({"items", "verdict"})),
    ("orchestrator", frozenset({"action", "node_id", "reason"})),
    ("survey", frozenset({"boundaries"})),
    ("contract", frozenset({"rules"})),
)


def role_of_schema(schema: dict[str, Any]) -> str:
    """Classify one recorded call by its schema's top-level property set.
    Unknown schemas report as ``"unknown"`` rather than crashing — a new
    call role added later must not break the harness's own report."""
    props = set((schema or {}).get("properties", {}))
    for role, required in _ROLE_BY_PROPS:
        if required <= props:
            return role
    return "unknown"


def call_input_tokens(call: tuple[list[dict[str, str]], dict[str, Any]]) -> int:
    """The input-token cost of one recorded call: the sum over messages,
    using the same ``estimate_tokens`` heuristic every budget check in the
    harness uses (so the number is comparable with the budgets the planner
    and the leaf gate reason about)."""
    messages, _schema = call
    return sum(estimate_tokens(str(message.get("content", ""))) for message in messages)


def calls_by_role(calls: list[tuple[list[dict[str, str]], dict[str, Any]]]) -> dict[str, int]:
    """Total calls per role, in the order the roles appear above. The
    per-tier number in the report is this summed per run, grouped by the
    tier the run classified into (see ``escalation_precision`` for the
    aggregation)."""
    counter: Counter[str] = Counter()
    for _messages, schema in calls:
        counter[role_of_schema(schema)] += 1
    return dict(sorted(counter.items()))


# ----------------------------------------------------------------------
# Per-run measurements over the run directory
# ----------------------------------------------------------------------

_TERMINAL_EVENT_TYPES = ("episode_completed", "node_redispatched")


def _read_events(run_dir: Path) -> list[dict[str, Any]]:
    from ..v0.events import EventLog
    from ..v0.run_dir import events_path

    try:
        return EventLog(events_path(run_dir)).read_all()
    except OSError:
        return []


def terminal_events_per_node(run_dir: Path) -> dict[str, int]:
    """§13: "replay converges to exactly one artifact and one terminal
    event per node" — the load-bearing resume invariant. Counts
    ``episode_completed`` (the real terminal event) per node id; the
    resume check asserts this stays ≤1 after a second run over the same
    directory."""
    counts: Counter[str] = Counter()
    for event in _read_events(run_dir):
        if event.get("type") != "episode_completed":
            continue
        node_id = str(event.get("node_id", ""))
        if node_id:
            counts[node_id] += 1
    return dict(counts)


def escalation_events(run_dir: Path) -> list[dict[str, Any]]:
    """Every ``run_tier_escalated`` event in log order — the raw material
    of escalation precision. An empty list across a set of varied tasks is
    itself a finding (§C5: "zero escalation across varied tasks means it
    is too conservative"), which is why the report prints the count even
    when it is zero."""
    events: list[dict[str, Any]] = []
    for event in _read_events(run_dir):
        if event.get("type") != "run_tier_escalated":
            continue
        events.append(
            {
                "trigger": str(event.get("trigger", "")),
                "from": str(event.get("from", "")),
                "to": str(event.get("to", "")),
                "node_id": str(event.get("node_id", "")) if event.get("node_id") else "",
            }
        )
    return events


def approval_rate_by_shape(run_dir: Path) -> dict[str, dict[str, Any]]:
    """§C5's "approval rate segmented by shape". Reads approvals.jsonl's
    pilot records (context carries the node's ``shape``) and, per shape,
    reports how many pilots were answered, how many were accepted as-is
    (blank answer — the pilot passed untouched) vs edited (the operator
    changed the exemplar, which is the diff the contract gets derived
    from). A shape whose pilots all get edited is the "which exemplar to
    re-pilot" signal §14 names; a shape that never reaches the operator
    (no pilots at all) is reported as absent, not as 100%."""
    by_shape: dict[str, dict[str, Any]] = {}
    for approval in approval_store.read_all(run_dir):
        if approval.kind != "pilot":
            continue
        shape = str(approval.context.get("shape", "") or "unknown")
        entry = by_shape.setdefault(shape, {"count": 0, "resolved": 0, "accepted_as_is": 0, "edited": 0})
        entry["count"] += 1
        if approval.status != "resolved":
            continue
        entry["resolved"] += 1
        if approval.user_input.strip():
            entry["edited"] += 1
        else:
            entry["accepted_as_is"] += 1
    for entry in by_shape.values():
        entry["accept_rate"] = (
            round(entry["accepted_as_is"] / entry["resolved"], 3) if entry["resolved"] else 0.0
        )
    return dict(sorted(by_shape.items()))


# ----------------------------------------------------------------------
# Per-leaf prompt segments (the §C5 segment instrument)
# ----------------------------------------------------------------------

def _leaf_nodes(run_dir: Path) -> list[TaskNode]:
    """The dispatched leaves of a run: tree.json's nodes when a plan
    produced a tree, otherwise the T0/T1 direct node (direct_node.json).
    The same two sources the driver itself reads."""
    tree = tree_path(run_dir)
    if tree.exists():
        from ..v1.tree import TaskTree

        return list(TaskTree.load(tree).nodes.values())
    direct = direct_node_path(run_dir)
    if direct.exists():
        from ..v1.tree import TaskTree

        loaded = TaskTree.load(direct)
        return list(loaded.nodes.values())
    return []


def per_leaf_segment_tokens(
    run_dir: Path,
    *,
    inline_spans: bool = True,
) -> list[dict[str, int]]:
    """The §C5 "mean input tokens per leaf broken down by prompt segment"
    instrument, applied post-hoc: rebuild each leaf's prompt with
    ``build_node_prompt``'s segment callback and record ``{label: tokens}``
    per leaf. Prompt assembly is deterministic and depends only on
    (node, run_dir), so the rebuilt prompt is byte-identical to the one
    the Writer actually saw — no driver changes were needed to instrument
    it."""
    rows: list[dict[str, int]] = []
    for node in _leaf_nodes(run_dir):
        per_segment: dict[str, int] = {}
        build_node_prompt(
            node,
            run_dir,
            inline_spans=inline_spans,
            segment_tokens=lambda label, tokens: per_segment.__setitem__(label, tokens),
        )
        rows.append(per_segment)
    return rows


def mean_tokens_by_segment(run_dir: Path, *, inline_spans: bool = True) -> dict[str, float]:
    """Column means over ``per_leaf_segment_tokens``, ordered by the
    prompt-assembly order of the labels (§8: goal_and_rubric first, retry
    last — see ``prompts.build_node_prompt``). Labels
    absent from a leaf are not counted as zeros — a leaf with no contract
    did not pay for a contract segment, and padding it with zeros would
    understate the mean for leaves that did."""
    rows = per_leaf_segment_tokens(run_dir, inline_spans=inline_spans)
    totals: dict[str, int] = {}
    counts: dict[str, int] = {}
    for row in rows:
        for label, tokens in row.items():
            totals[label] = totals.get(label, 0) + tokens
            counts[label] = counts.get(label, 0) + 1
    means = {label: round(totals[label] / counts[label], 1) for label in totals}
    order = ["brief", "artifact_instruction", "goal_and_rubric", "contract",
             "inputs", "spans", "promotions", "judgment_rubric", "retry"]
    return {label: means[label] for label in order if label in means}


# ----------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------

def escalation_precision(measurements: list[dict[str, Any]]) -> dict[str, Any]:
    """§C5's escalation precision across the runs of one task (or the
    whole suite): how often the classification held up without an
    escalation. ``precision = correct / runs`` where correct means the
    run's final tier equals its measured tier. The report keeps the raw
    triggers too — precision alone cannot distinguish "two runs, one
    operator-requested escalate" from "two runs, one size-defect
    escalation", and the two call for opposite responses."""
    total = len(measurements)
    if total == 0:
        return {"runs": 0, "precision": None, "escalated_runs": 0, "triggers": []}
    correct = sum(1 for m in measurements if m["tier_final"] == m["tier_measured"])
    triggers: Counter[str] = Counter()
    for m in measurements:
        for event in m.get("escalations", []):
            triggers[event["trigger"]] += 1
    return {
        "runs": total,
        "precision": round(correct / total, 3),
        "escalated_runs": total - correct,
        "triggers": dict(sorted(triggers.items())),
    }


def summarize_calls_by_tier(measurements: list[dict[str, Any]]) -> dict[str, Any]:
    """§C5's calls-by-tier headline: per tier (measured), the mean and
    total provider calls across its runs, plus the per-role breakdown of
    the totals. Tiers that never ran report zero runs, not a fabricated
    mean."""
    by_tier: dict[str, list[dict[str, Any]]] = {}
    for m in measurements:
        by_tier.setdefault(m["tier_measured"], []).append(m)
    summary: dict[str, Any] = {}
    for tier in sorted(by_tier, key=lambda t: (len(t), t)):
        runs = by_tier[tier]
        total = sum(run["total_calls"] for run in runs)
        roles: Counter[str] = Counter()
        for run in runs:
            for role, count in run["calls_by_role"].items():
                roles[role] += count
        summary[tier] = {
            "runs": len(runs),
            "mean_calls": round(total / len(runs), 2),
            "total_calls": total,
            "calls_by_role": dict(sorted(roles.items())),
        }
    return summary
