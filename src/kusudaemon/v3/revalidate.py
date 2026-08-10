"""Contract-amendment re-validation (PLAN.md §10, §15.5).

A user amendment (``v2/contract.amend_contract``) changes the rules
downstream of nodes that already passed under the old contract — §10:
"completed nodes now stale". Blanket-regenerating everything is explicitly
rejected: "Do *not* blanket-regenerate." Instead, re-run the existing
Reviewer — read-only, stateless, **no writers dispatched** — against the
amended contract, and triage each already-passed node into:

- **clean** — already satisfies the amendment, no action.
- **patchable** — small scoped edit closes the gap (an additive amendment:
  "every unit needs a summary box" → append one).
- **regenerate** — the amendment contradicts what's written ("worked
  solutions → hints-only"), no small edit fixes it.

§10 also requires showing a cost estimate *before* running the pass
(``estimate_revalidation_cost``) and presenting triage counts for approval
*before* executing any repair (``summarize_triage``) — this module only
performs the read-only classification; ``apply_revalidation_triage`` is a
separate, explicit call so a caller can insert that approval gate between
them exactly as §10 describes ("Present counts, get approval, then
execute").

Patchable and regenerate both execute through ``repair.run_repair`` — a
regenerate is simply a repair whose prompt asks for a full rewrite instead
of a minimal edit (``repair.RepairMode``), not a separate code path; the
"scoped, located defect" both share is here derived straight from the
reviewer verdict's own ``defect``/``id`` fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from ..adapters.base import AgentAdapter
from ..environment.base import Environment
from ..types import EpisodeBudget
from ..v0.events import EventLog
from ..v0.run_dir import node_artifact_path
from ..v1.gates import estimate_tokens
from ..v1.provider import OpenAICompatibleProvider
from ..v1.reviewer import VERDICT_SCHEMA, ReviewVerdict
from ..v1.tree import TaskNode, TaskTree
from .prefilter import artifact_may_be_affected
from .repair import RepairOutcome, run_repair
from .run_dir import revalidation_audit_path

AdapterFactory = Callable[[TaskNode], AgentAdapter]
Classification = Literal["clean", "patchable", "regenerate"]

_REVALIDATE_SYSTEM_PROMPT = (
    "You are the Reviewer in a long-horizon task harness, re-checking an "
    "already-approved artifact against a contract amendment made after it "
    "was produced. You have not seen how it was produced and cannot "
    "rewrite it. For each rubric/contract item, say whether the artifact "
    "already complies (pass) or not; for anything failing, classify the "
    "fix as 'patchable' (a small, additive, scoped edit closes the gap) or "
    "'regenerate' (the amendment contradicts what's already written — no "
    "small edit fixes it). Never invent items outside the given rubric/"
    "contract. Respond with a single JSON object only."
)


@dataclass
class RevalidationEstimate:
    node_count: int
    estimated_tokens: int
    skipped_count: int = 0
    """Nodes the lexical pre-filter (PLAN-zeromem.md §2) would skip — they
    contribute 0 tokens and increment ``skipped_count``, so the operator's
    cost preview reflects what will actually be spent."""


def estimate_revalidation_cost(
    run_dir: str | Path,
    tree: TaskTree,
    contract_text: str,
    *,
    amendment_text: str | None = None,
    prefilter: bool = True,
) -> RevalidationEstimate:
    """§10: "Cost ≈ N × (contract + rubric + artifact). Show that estimate
    before running." — a pure token count, no model call. When the §2
    pre-filter is enabled and an ``amendment_text`` is supplied, nodes the
    filter would skip contribute 0 tokens and count toward
    ``skipped_count``."""
    run_dir = Path(run_dir)
    contract_tokens = estimate_tokens(contract_text)
    passed = [n for n in tree.nodes.values() if n.status == "passed"]
    total = 0
    skipped = 0
    for node in passed:
        rubric_text = "\n".join(node.rubric.get(j, "") for j in node.judgment)
        artifact_text = _read_artifact(run_dir, node.id)
        if prefilter and amendment_text:
            needs_review, _ = artifact_may_be_affected(
                amendment_text, artifact_text, rubric_text
            )
            if not needs_review:
                skipped += 1
                continue
        total += contract_tokens + estimate_tokens(rubric_text) + estimate_tokens(artifact_text)
    return RevalidationEstimate(
        node_count=len(passed), estimated_tokens=total, skipped_count=skipped
    )


@dataclass
class Triage:
    node_id: str
    classification: Classification
    verdict: ReviewVerdict


def classify_verdict(verdict: ReviewVerdict) -> Classification:
    if verdict.verdict == "pass":
        return "clean"
    failing_classes = {
        item.get("class") for item in verdict.items if not item.get("pass", True)
    }
    # A failing item with no class, or any item explicitly needing a
    # rewrite, is not safely patchable — default to the stricter bucket.
    if failing_classes and failing_classes <= {"patchable"}:
        return "patchable"
    return "regenerate"


def _read_artifact(run_dir: Path, node_id: str) -> str:
    path = node_artifact_path(run_dir, node_id)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _review_against_contract(
    node: TaskNode, artifact_text: str, contract_text: str, provider: OpenAICompatibleProvider
) -> ReviewVerdict:
    rubric_lines = (
        "\n".join(f"{j}: {node.rubric.get(j, '')}" for j in node.judgment)
        or "(no per-node judgment items)"
    )
    messages = [
        {"role": "system", "content": _REVALIDATE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Contract (amended):\n{contract_text}\n\n"
                f"Node rubric:\n{rubric_lines}\n\nArtifact:\n{artifact_text}"
            ),
        },
    ]
    payload = provider.complete_json(messages, VERDICT_SCHEMA)
    return ReviewVerdict(
        node_id=node.id,
        items=list(payload.get("items", [])),
        verdict=str(payload.get("verdict", "fail")),
    )


def revalidate_node(
    run_dir: str | Path, node: TaskNode, contract_text: str, provider: OpenAICompatibleProvider
) -> Triage:
    artifact_text = _read_artifact(Path(run_dir), node.id)
    verdict = _review_against_contract(node, artifact_text, contract_text, provider)
    return Triage(node_id=node.id, classification=classify_verdict(verdict), verdict=verdict)


def run_revalidation_pass(
    run_dir: str | Path,
    tree: TaskTree,
    tree_path: str | Path,
    contract_text: str,
    provider: OpenAICompatibleProvider,
    *,
    node_ids: list[str] | None = None,
    amendment_text: str | None = None,
    prefilter: bool = True,
) -> dict[str, Triage]:
    """Read-only: no writer is dispatched here (§10: "no writers
    dispatched"). Marks anything not clean ``"stale"`` in the tree and
    returns the full triage map so the caller can present counts before
    calling ``apply_revalidation_triage``.

    ``amendment_text``/``prefilter`` (PLAN-zeromem.md §2): when both are
    given, the lexical pre-filter runs first and skips nodes the amendment
    provably cannot bear on — those come back ``"clean"`` without a model
    call, recorded to the same audit file with a ``prefiltered`` flag so a
    skip is auditable rather than invisible. The filter only ever produces
    clean; patchable/regenerate still require the Reviewer."""
    run_dir = Path(run_dir)
    targets = node_ids or [n.id for n in tree.nodes.values() if n.status == "passed"]
    triage_by_node: dict[str, Triage] = {}
    for node_id in targets:
        node = tree.nodes[node_id]
        if prefilter and amendment_text:
            rubric_text = "\n".join(node.rubric.get(j, "") for j in node.judgment)
            needs_review, reason = artifact_may_be_affected(
                amendment_text, _read_artifact(run_dir, node_id), rubric_text
            )
            if not needs_review:
                triage = Triage(
                    node_id=node_id,
                    classification="clean",
                    verdict=ReviewVerdict(node_id=node_id, items=[], verdict="pass"),
                )
                triage_by_node[node_id] = triage
                revalidation_audit_path(run_dir, node_id).write_text(
                    _triage_json(triage, skipped_reason=reason), encoding="utf-8"
                )
                continue
        triage = revalidate_node(run_dir, node, contract_text, provider)
        triage_by_node[node_id] = triage
        revalidation_audit_path(run_dir, node_id).write_text(
            _triage_json(triage), encoding="utf-8"
        )
        if triage.classification != "clean":
            node.status = "stale"
    tree.save(tree_path)
    return triage_by_node


def summarize_triage(triage_by_node: dict[str, Triage]) -> dict[str, int]:
    counts = {"clean": 0, "patchable": 0, "regenerate": 0}
    for triage in triage_by_node.values():
        counts[triage.classification] += 1
    return counts


def _defect_from_verdict(verdict: ReviewVerdict) -> str:
    lines = [
        f"{item.get('id', '?')}: {item.get('defect', '')}".rstrip(": ")
        for item in verdict.items
        if not item.get("pass", True)
    ]
    return "\n".join(lines) if lines else "amended contract no longer satisfied"


async def apply_revalidation_triage(
    run_dir: str | Path,
    tree: TaskTree,
    tree_path: str | Path,
    manifest_path: str | Path,
    triage_by_node: dict[str, Triage],
    writer_adapter_factory: AdapterFactory,
    env: Environment,
    provider: OpenAICompatibleProvider,
    log: EventLog,
    *,
    writer_budget: EpisodeBudget | None = None,
    max_attempts: int = 3,
) -> list[RepairOutcome]:
    """The execution half of §10's triage — call only after the counts from
    ``summarize_triage`` have been presented and approved. "Clean" nodes are
    left untouched (still "passed"); everything else is dispatched through
    ``repair.run_repair`` under the mode its triage implies."""
    budget = writer_budget or EpisodeBudget()
    outcomes: list[RepairOutcome] = []
    for node_id, triage in triage_by_node.items():
        if triage.classification == "clean":
            continue
        node = tree.nodes[node_id]
        adapter = writer_adapter_factory(node)
        mode = "patch" if triage.classification == "patchable" else "regenerate"
        outcome = await run_repair(
            run_dir, node, tree, tree_path, manifest_path,
            _defect_from_verdict(triage.verdict),
            adapter, env, budget, provider, log,
            mode=mode, max_attempts=max_attempts,
        )
        outcomes.append(outcome)
    return outcomes


def _triage_json(triage: Triage, skipped_reason: str | None = None) -> str:
    payload: dict[str, Any] = {
        "node": triage.node_id,
        "classification": triage.classification,
        "items": triage.verdict.items,
        "verdict": triage.verdict.verdict,
    }
    if skipped_reason:
        # PLAN-zeromem.md §2.5: a pre-filter skip is recorded to the same
        # audit file a model verdict would write, but flagged so it cannot
        # be mistaken for one.
        payload["prefiltered"] = True
        payload["reason"] = skipped_reason
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
