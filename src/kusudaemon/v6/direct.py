"""T0/T1 direct execution paths (PLAN.md §A4.3, §B2).

**T0** ("1 episode, 1 review call, no tree file") never gets a
``tree.json`` — its whole job is one gated Writer episode plus one Reviewer
verdict. It still needs *some* durable, resumable record of that one node's
state (a crash between dispatch and review must resume correctly, same as
every other tier), so it gets its own file, ``direct_node.json`` — the same
``TaskTree`` JSON-array serialization ``tree.json`` uses, just never named
``tree.json`` and never read by ``pipeline/driver.py:_phase_done("plan")``,
which is what "no tree file" actually protects: a T0 run must never look,
to a resuming driver, like planning already happened.

Dispatch and review reuse ``v1/round_loop.py``'s ``dispatch_node``/
``review_and_transition_node`` directly (PLAN.md §3: "don't reinvent
episode dispatch") against an in-memory, single-node ``TaskTree`` — the
exact same gate-cache write, manifest line, and status transition a real
round-loop dispatch produces, so nothing downstream (the dashboard,
``manifest.jsonl`` readers, ``audit/<node>.json`` consumers) needs a
tree-less special case to render a T0 run.

**T1** ("1 node, ≤2 explorers") gets a real ``tree.json`` — unlike T0, its
phase list has no "plan" phase to skip, so the one node it needs is built
directly by code from the goal (``build_single_node_tree``) rather than
spent on a Planner call that has nothing to partition. Once that one-node
tree is on disk, it flows through the *unmodified* ``run_round_loop`` —
which already handles a 1-node tree correctly today, no T1-specific code
needed there at all.
"""

from __future__ import annotations

from pathlib import Path

from ..v0.events import EventLog
from ..v0.run_dir import manifest_path
from ..types import EpisodeBudget
from ..v1.round_loop import AdapterFactory, PromptBuilder, dispatch_node, review_and_transition_node
from ..v1.tree import NodeBudget, TaskNode, TaskTree

DIRECT_NODE_ID = "direct"
SINGLE_NODE_ID = "single"

# PLAN.md §A4.4: "T0/T1 node fails gates twice with a size defect ->
# promote to T2." T0/T1 exist for goals small enough that "give it three
# tries and hope" is the wrong instinct -- either the node is small enough
# to land in ~2 attempts, or the estimate was wrong and it should escalate
# to real planning rather than burn a third attempt at the same size. This
# is a separate, smaller ceiling than v1/round_loop.py's own
# `max_attempts` (default 3, still used by T2/T3's round loop, which stays
# tier-agnostic -- CLAUDE.md/PLAN.md's own stated design constraint).
DIRECT_MAX_ATTEMPTS = 2

# A gate result's `.gate` string is the raw gate spec ("max_tokens:24000"),
# and `_transition_after_writer` joins `f"{gate}: {detail}"` into
# `last_defect` (v1/round_loop.py) -- so a size-class failure's defect text
# always contains this substring. No runtime "calls exceeded" gate exists
# today (driver.py: "NodeBudget.calls stays deliberately unwired"), so
# max_tokens is the only size-class defect a T0/T1 dispatch can produce.
_SIZE_DEFECT_MARKER = "max_tokens:"


def is_size_defect(last_defect: str) -> bool:
    return _SIZE_DEFECT_MARKER in last_defect


def direct_node_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "direct_node.json"


def build_direct_node(
    goal: str,
    *,
    node_id: str = DIRECT_NODE_ID,
    token_budget: int = 24_000,
    tool_call_cap: int = 15,
    inputs: tuple[str, ...] = (),
) -> TaskNode:
    """One node, built directly from the goal by code -- no Planner call.
    Matches v2/planner.py's leaf defaults (`nonempty` + `max_tokens:N`,
    empty judgment -- review auto-passes when there is nothing to ask an
    opinion about, v1/reviewer.py) so a T0/T1 node behaves exactly like any
    other leaf everywhere except in how it got built.

    §E28 (2026-08-13): ``inputs`` names the corpus for kind="text" runs
    (stored relative to run_dir per §D0b, e.g. ``("source.txt",)``). Before
    this, T0/T1 nodes shipped ``inputs=[]`` -- the prompt builder's Inputs
    section was empty, so a corpus run's writer never saw the corpus and
    degenerated into repetition (observed live against a 129.8 MB
    source.txt)."""
    return TaskNode(
        id=node_id,
        brief=goal,
        artifact=f"out/{node_id}.md",
        gates=["nonempty", f"max_tokens:{token_budget}"],
        budget=NodeBudget(tokens=token_budget, calls=tool_call_cap),
        inputs=inputs,
    )


def build_single_node_tree(goal: str, **kwargs) -> TaskTree:
    """T1's whole tree: one node, built directly from the goal (module
    docstring: a Planner call would have nothing to partition)."""
    node = build_direct_node(goal, node_id=SINGLE_NODE_ID, **kwargs)
    return TaskTree(nodes={node.id: node})


def _load_or_create_direct_node(run_dir: Path, goal: str, inputs: tuple[str, ...] = ()) -> tuple[TaskNode, TaskTree]:
    path = direct_node_path(run_dir)
    if path.exists():
        tree = TaskTree.load(path)
        return tree.nodes[DIRECT_NODE_ID], tree
    node = build_direct_node(goal, inputs=inputs)
    tree = TaskTree(nodes={node.id: node})
    tree.save(path)
    return node, tree


async def run_direct_episode(
    run_dir: str | Path,
    goal: str,
    provider,
    *,
    writer_adapter_factory: AdapterFactory,
    env,
    prompt_for_node: PromptBuilder,
    budget: EpisodeBudget,
    log: EventLog,
    max_attempts: int = DIRECT_MAX_ATTEMPTS,
    inputs: tuple[str, ...] = (),
) -> TaskNode:
    """T0's whole execute phase: one gated Writer episode, one Reviewer
    verdict (free when the node declares no judgment items, exactly like
    every other leaf today), no ``tree.json``. Idempotent/resumable the
    same way ``run_round_loop`` is — calling this again against a node
    already "passed"/"blocked" is a cheap no-op read, and a crash between
    dispatch and review resumes from ``direct_node.json``'s durable status
    exactly like a round-loop resume does from ``tree.json``.
    """
    run_dir = Path(run_dir)
    node, tree = _load_or_create_direct_node(run_dir, goal, inputs=inputs)
    path = direct_node_path(run_dir)
    manifest = manifest_path(run_dir)

    if node.status in ("passed", "blocked"):
        return node

    if node.status in ("pending", "dispatched"):
        node.status = "dispatched"
        tree.save(path)
        await dispatch_node(
            run_dir, node, tree, path,
            writer_adapter_factory=writer_adapter_factory, env=env,
            prompt_for_node=prompt_for_node, budget=budget, manifest=manifest,
            max_attempts=max_attempts, log=log,
        )
    if node.status == "awaiting_review":
        await review_and_transition_node(
            run_dir, node, tree, path, provider=provider, max_attempts=max_attempts, log=log
        )
    # Same "retry in place" shape as run_round_loop's own tail loop
    # (v1/round_loop.py) -- a failed attempt with attempts left is a
    # correction (last_defect carried forward into the next prompt via
    # pipeline/prompts.py), not a fresh dispatch decision.
    while node.status == "pending" and node.attempts < max_attempts:
        node.status = "dispatched"
        tree.save(path)
        await dispatch_node(
            run_dir, node, tree, path,
            writer_adapter_factory=writer_adapter_factory, env=env,
            prompt_for_node=prompt_for_node, budget=budget, manifest=manifest,
            max_attempts=max_attempts, log=log,
        )
        if node.status == "awaiting_review":
            await review_and_transition_node(
                run_dir, node, tree, path, provider=provider, max_attempts=max_attempts, log=log
            )
    return node
