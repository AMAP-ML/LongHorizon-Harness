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

**The split-proposal hint (PLAN.md §A8/§B5) is conditional, not a standing
instruction.** ``_inputs_exceed_budget`` recomputes, before every dispatch,
whether this node's own resolved ``inputs`` already exceed
``node.budget.tokens`` — the exact same measurement
``v7/split.py:evaluate_split``'s precondition 1 makes independently (this
module cannot import that one; see ``v1/round_loop.py``'s docstring for
the layering reason). Only when it's already true does ``writer_prompt``
append the split-option instruction: "a node that cannot legally split is
never told it can" (§B5) — telling every node it may split would invite
the exact failure mode §A2 invariant 2 exists to forbid (a model splitting
to avoid work), and ``v7/split.py``'s own gate would reject the proposal
anyway once it arrived, wasting the attempt this hint is meant to help
avoid wasting.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..adapters.base import AgentAdapter
from ..environment.base import Environment
from ..types import EpisodeBudget, EpisodeResult
from ..v0.run_dir import node_scratch_dir, resolve_stored
from ..v0.runner import run_node
from .gates import estimate_tokens
from .manifest import cap_promotion
from .tree import TaskNode

_ARTIFACT_INSTRUCTION = (
    "\n\nDo not close with a status update, a summary of what you wrote, or an "
    "offer to make changes — the artifact file itself, saved with your file "
    "tools, is what gets read next."
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

_SPLIT_INSTRUCTION_TEMPLATE = (
    "\n\nYour declared inputs already exceed this node's token budget. If you "
    "genuinely cannot complete the brief within budget, you may instead write "
    "{split_path} as a JSON object proposing a partition: "
    '{{"reason": "<why this must split>", "children": ['
    '{{"id": "<short id>", "brief": "<its own checkable done-condition>", '
    '"inputs": ["<subset of your own inputs this child covers>"], '
    '"estimated_calls": <int>}}, ...]}}. Every one of your inputs must be '
    "claimed by exactly one child, and there must be 2-8 children. The harness "
    "decides whether the split is accepted — do not assume it will be, and do "
    "not propose one just because the work feels large; only propose it if you "
    "cannot produce the artifact within budget."
)


def writer_prompt(
    brief_prompt: str, promotion_path: Path, *, split_hint_path: Path | None = None
) -> str:
    text = (
        brief_prompt
        + _ARTIFACT_INSTRUCTION
        + _PROMOTION_INSTRUCTION_TEMPLATE.format(promotion_path=promotion_path)
    )
    if split_hint_path is not None:
        text += _SPLIT_INSTRUCTION_TEMPLATE.format(split_path=split_hint_path)
    return text


def _inputs_exceed_budget(run_dir: Path, node: TaskNode) -> bool:
    """Same measurement as ``v7/split.py:evaluate_split``'s precondition 1
    (module docstring) — joined resolved input text, one ``estimate_tokens``
    call, compared against the node's own budget."""
    if not node.inputs:
        return False
    joined = "\n".join(
        _read_resolved(run_dir, ref) for ref in node.inputs
    )
    return estimate_tokens(joined) > node.budget.tokens


def _read_resolved(run_dir: Path, ref: str) -> str:
    try:
        return resolve_stored(run_dir, ref).read_text(encoding="utf-8")
    except OSError:
        return ""


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
    scratch_dir = node_scratch_dir(run_dir, node.id)
    promotion_path = scratch_dir / "promotion.json"
    # §11.9: a retry that ignores the promotion instruction must not inherit
    # the previous attempt's handoff — that would put attempt 1's text in
    # this attempt's manifest line. Unlink before dispatch, the same way
    # v0/runner.py unlinks trace.jsonl before a redispatch.
    if promotion_path.exists():
        promotion_path.unlink()

    split_hint_path = scratch_dir / "split.json" if _inputs_exceed_budget(run_dir, node) else None
    result = await run_node(
        run_dir,
        node.id,
        writer_prompt(prompt, promotion_path, split_hint_path=split_hint_path),
        adapter,
        env,
        budget,
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
