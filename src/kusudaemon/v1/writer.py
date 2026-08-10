"""Writer node execution for the v1 round loop (PLAN.md §3, §5, §13).

Wraps v0's resumable ``run_node`` unchanged — session capture, crash
resume, and the resume-after-complete no-op all come along for free — and
adds the one thing v1 needs on top: the Writer's handoff back to the
harness is capped at ~400 tokens (§13 v1 scope: "Writer returns capped at
~400 tokens").

Per-node tool restriction (§5: "tools is per-node... the single biggest
token lever") is *not* done here. It lives in how the caller builds the
``adapter`` it hands in — see ``round_loop.py`` and
``ClaudeCodeAdapter(allowed_tools=...)`` — because the Claude Code CLI
bakes its tool policy into the command line at adapter construction time,
before any single episode runs.

v1 has no template/type system yet (v2's planner), so there's no in-band
structured-output channel for the writer to hand back JSON the way
Orchestrator/Reviewer do — it's still a full agent-CLI loop. Instead the
prompt asks the agent to write a short handoff to
``scratch/<node>/promotion.json``. If it doesn't (an older prompt, a model
that ignores the instruction), the harness falls back to the episode's own
visible output/log so the cap is still enforced either way.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..adapters.base import AgentAdapter
from ..environment.base import Environment
from ..types import EpisodeBudget, EpisodeResult
from ..v0.run_dir import node_scratch_dir
from ..v0.runner import run_node
from .manifest import cap_promotion
from .tree import TaskNode

_ARTIFACT_INSTRUCTION = (
    "\n\nProduce the full artifact text as your final answer — your last message in "
    "this conversation becomes the artifact file verbatim. Do not close with a "
    "status update, a summary of what you wrote, or an offer to make changes: write "
    "the finished section itself, in full, as your last message."
)

_PROMOTION_INSTRUCTION_TEMPLATE = (
    "\n\nWhen you are finished, also write a short handoff note to {promotion_path} "
    'as a JSON object: {{"promotion": "<=400 tokens summarizing what you produced, '
    'key decisions, and anything a downstream node should know"}}. A document-level '
    "reviewer sees only this summary, never your artifact's full text, when it later "
    "checks the whole document for coverage gaps, duplication, and contract "
    "compliance — so state what this section actually covers and asserts, not just "
    "that it's done."
)


def writer_prompt(brief_prompt: str, promotion_path: Path) -> str:
    return (
        brief_prompt
        + _ARTIFACT_INSTRUCTION
        + _PROMOTION_INSTRUCTION_TEMPLATE.format(promotion_path=promotion_path)
    )


async def run_writer_node(
    run_dir: str | Path,
    node: TaskNode,
    prompt: str,
    adapter: AgentAdapter,
    env: Environment,
    budget: EpisodeBudget,
) -> tuple[EpisodeResult, str]:
    """Run (or resume) one Writer node. Returns (episode result, capped promotion)."""
    run_dir = Path(run_dir)
    promotion_path = node_scratch_dir(run_dir, node.id) / "promotion.json"

    result = await run_node(
        run_dir, node.id, writer_prompt(prompt, promotion_path), adapter, env, budget
    )

    promotion = _read_promotion(promotion_path)
    if promotion is None:
        promotion = result.metadata.get("assistant_visible_output") or result.actions_log or ""
    return result, cap_promotion(promotion)


def _read_promotion(promotion_path: Path) -> str | None:
    if not promotion_path.exists():
        return None
    try:
        data = json.loads(promotion_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("promotion")
    return value if isinstance(value, str) else None
