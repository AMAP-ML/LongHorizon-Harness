"""Pilot — the consistency mechanism (PLAN.md §4.4).

Sizing classifies nodes by shape; run **one pilot per shape**
(``select_pilot_nodes``), not "the first node" — the first chapter of
anything is atypical, so this picks the shape's median-by-id node rather
than whichever sorts first.

For each pilot: the Writer produces the artifact via v1's
``run_writer_node`` (reused unmodified — pilot writing isn't special), then
the harness records a durable ``pilot_awaiting_approval`` event and
returns. This is a durable event-log state, not a blocking prompt (§4.4:
"the user can return the next morning") — resuming is just calling
``approve_pilot`` whenever the edit is ready, no different in kind from v0's
crash-resume.

``approve_pilot`` is that resume point: it takes the user's on-disk edit,
diffs it against what the Writer produced, writes the edit back as the
canonical artifact, and turns the diff into contract rules via one small
model call — inferring "user deleted every historical aside" from a diff
needs judgment a heuristic can't supply (§4.4: "the diff is the
highest-signal input in the whole system"). An empty diff derives no rules
and spends no model call.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from ..adapters.base import AgentAdapter
from ..environment.base import Environment
from ..types import EpisodeBudget
from ..v0.events import EventLog
from ..v0.run_dir import node_artifact_path
from ..v1.provider import OpenAICompatibleProvider
from ..v1.tree import TaskNode, TaskTree
from ..v1.writer import run_writer_node
from .contract import ContractRule

DERIVE_RULES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["rules"],
    "additionalProperties": False,
    "properties": {
        "rules": {"type": "array", "items": {"type": "string", "maxLength": 200}},
    },
}

_DERIVE_SYSTEM_PROMPT = (
    "You are deriving authoring rules from one user edit of a pilot "
    "artifact. You are shown the original Writer output and a unified diff "
    "of the user's changes. Infer general, reusable rules the edit implies "
    "— never a description of the diff itself. Example: if every "
    "historical aside was deleted, the rule is 'exclude "
    "historical/biographical material', not 'the user deleted paragraph "
    "3'. Keep each rule to one terse imperative sentence, in the style "
    "'define every bolded term from source span'. If the diff implies "
    "nothing generalizable, return an empty list. Respond with a single "
    "JSON object only."
)


def select_pilot_nodes(tree: TaskTree) -> dict[str, TaskNode]:
    """One representative node per distinct shape present in the tree — the
    node closest to the middle of that shape's id-sorted list, not the
    first (§4.4: "the first chapter of anything is atypical")."""
    by_shape: dict[str, list[TaskNode]] = {}
    for node in tree.nodes.values():
        by_shape.setdefault(node.shape, []).append(node)
    selected: dict[str, TaskNode] = {}
    for shape, candidates in by_shape.items():
        ordered = sorted(candidates, key=lambda node: node.id)
        selected[shape] = ordered[len(ordered) // 2]
    return selected


def _pilot_original_path(run_dir: str | Path, node_id: str) -> Path:
    """Where the Writer's pre-edit pilot output is preserved, alongside
    repair snapshots (§11.3: the path the operator edits — the live
    artifact — cannot double as the diff's 'original')."""
    path = Path(run_dir) / "out" / ".versions" / node_id
    path.mkdir(parents=True, exist_ok=True)
    return path / "pilot-original.md"


def snapshot_pilot_original(run_dir: str | Path, node_id: str, text: str) -> Path:
    """Persist the Writer's output before any operator edit. Only writes
    when no snapshot exists, so a resume after a crash mid-phase cannot
    clobber the true original with the (possibly edited) artifact."""
    path = _pilot_original_path(run_dir, node_id)
    if not path.exists():
        path.write_text(text, encoding="utf-8")
    return path


async def run_pilot(
    run_dir: str | Path,
    node: TaskNode,
    prompt: str,
    adapter: AgentAdapter,
    env: Environment,
    budget: EpisodeBudget,
    log: EventLog,
) -> str:
    """Run the pilot Writer episode and record the durable awaiting-approval
    state. Returns the artifact text as produced, before any user edit."""
    await run_writer_node(run_dir, node, prompt, adapter, env, budget)
    artifact_text = _read_artifact(run_dir, node.id)
    snapshot_pilot_original(run_dir, node.id, artifact_text)
    log.append(
        {"node_id": node.id, "role": "harness", "round": 0, "type": "pilot_awaiting_approval"}
    )
    return artifact_text


def approve_pilot(
    run_dir: str | Path,
    node: TaskNode,
    edited_text: str,
    provider: OpenAICompatibleProvider,
    log: EventLog,
) -> list[ContractRule]:
    """Resume point for a pilot sitting in ``pilot_awaiting_approval``. The
    diff is taken against the Writer's snapshotted original, not the live
    artifact (which the operator edits on disk, §4.4); a run resumed from a
    snapshotless legacy state falls back to the live artifact."""
    snapshot_path = _pilot_original_path(run_dir, node.id)
    original = snapshot_path.read_text(encoding="utf-8") if snapshot_path.exists() else _read_artifact(run_dir, node.id)
    diff_text = _unified_diff(original, edited_text)
    node_artifact_path(run_dir, node.id).write_text(edited_text, encoding="utf-8")

    rule_texts = _derive_contract_rules(diff_text, original, provider) if diff_text.strip() else []
    log.append(
        {
            "node_id": node.id,
            "role": "harness",
            "round": 0,
            "type": "pilot_approved",
            "rules": rule_texts,
        }
    )
    return [ContractRule(source=node.id, shape=node.shape, text=text) for text in rule_texts]


def _read_artifact(run_dir: str | Path, node_id: str) -> str:
    path = node_artifact_path(run_dir, node_id)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _unified_diff(original: str, edited: str) -> str:
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        edited.splitlines(keepends=True),
        fromfile="pilot/original",
        tofile="pilot/edited",
    )
    return "".join(diff)


def _derive_contract_rules(
    diff_text: str, original: str, provider: OpenAICompatibleProvider
) -> list[str]:
    messages = [
        {"role": "system", "content": _DERIVE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Original artifact (excerpt):\n{original[:2000]}\n\nDiff:\n{diff_text[:4000]}"
            ),
        },
    ]
    payload = provider.complete_json(messages, DERIVE_RULES_SCHEMA)
    return [str(rule) for rule in payload["rules"]]
