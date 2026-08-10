"""Prompt assembly for writers (PLAN.md §11 "default node view: brief").

A Writer node's prompt is assembled entirely before its bounded episode
starts (§8 context discipline): brief, then the frozen contract (every
artifact must satisfy it — §4.4), then the node's ``inputs`` — materialized
spine-unit file paths under ``spine/`` (PLAN-zeromem.md §7) and, once v4
research ran, finding file paths — the agent is expected to read itself.
Nothing here is a model call; ``inputs`` are file paths the agent resolves
with its own tools. (Pre-§7 or unmaterialized runs fall back to a bare
unit id — see ``v2/survey.py:unit_input_path`` — which renders the same
way here; the agent just has nothing to open.)

Finally, if the node carries a ``last_defect`` from a prior failed attempt
(PLAN-zeromem.md §9), it's appended as a retry block — patch framing on
attempt 2, regenerate framing on attempt 3+, mirroring
``v3/repair.py``'s two ``RepairMode`` framings one layer earlier, before a
node has ever passed once.
"""

from __future__ import annotations

from pathlib import Path

from ..v1.tree import TaskNode
from ..v2.contract import load_contract

_PATCH_RETRY_INSTRUCTION = (
    "Your previous attempt at this node failed with the feedback below. Make "
    "the MINIMAL change necessary to fix it — do not rewrite or restructure "
    "anything else:\n"
)
_REGENERATE_RETRY_INSTRUCTION = (
    "Your previous attempts at this node failed with the feedback below, and "
    "a small patch has not been enough. Rewrite the artifact from scratch to "
    "address it:\n"
)


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
    if node.last_defect:
        # node.attempts is already incremented (by round_loop's transition)
        # before a retry is redispatched, so attempts==1 is building the
        # prompt for the node's 2nd dispatch, attempts>=2 for its 3rd+.
        instruction = _PATCH_RETRY_INSTRUCTION if node.attempts <= 1 else _REGENERATE_RETRY_INSTRUCTION
        parts.append(instruction + node.last_defect)
    return "\n\n".join(parts)