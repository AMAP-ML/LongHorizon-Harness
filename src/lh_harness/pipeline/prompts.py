"""Prompt assembly for writers (PLAN.md §11 "default node view: brief").

A Writer node's prompt is assembled entirely before its bounded episode
starts (§8 context discipline): brief, then the frozen contract (every
artifact must satisfy it — §4.4), then the node's ``inputs`` (spine unit
ids and, once v4 research ran, finding file paths the agent is expected to
read itself). Nothing here is a model call; ``inputs`` are file paths and
ids the agent resolves with its own tools.
"""

from __future__ import annotations

from pathlib import Path

from ..v1.tree import TaskNode
from ..v2.contract import load_contract


def build_node_prompt(node: TaskNode, run_dir: str | Path) -> str:
    parts = [f"Your brief: {node.brief}"]
    contract = load_contract(run_dir).strip()
    if contract:
        parts.append(
            "Global contract — every artifact you produce must satisfy it:\n" + contract
        )
    if node.inputs:
        parts.append(
            "Inputs (read them with your tools before writing, and cite them "
            "where relevant):\n" + "\n".join(f"- {item}" for item in node.inputs)
        )
    if node.judgment and node.rubric:
        rubric_lines = "\n".join(
            f"- {judgment_id}: {node.rubric[judgment_id]}"
            for judgment_id in node.judgment
            if judgment_id in node.rubric
        )
        parts.append(f"Judgment rubric the Reviewer will hold you to:\n{rubric_lines}")
    return "\n\n".join(parts)