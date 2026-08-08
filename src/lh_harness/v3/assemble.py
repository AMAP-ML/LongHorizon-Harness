"""Concatenation + index (PLAN.md §4.6.1) — the first of the three assembly
steps, and one of the two that need no model at all.

Ordering comes from ``tree.json`` itself, exactly as §4.6.1 specifies
("Generate main.tex (or equivalent) with \\input{} lines ordered from
tree.json"): ``TaskTree.nodes`` is a plain dict, but it's built by
``TaskTree.load`` via a dict comprehension over the JSON array in file
order, and the planner (``v2/planner.py``) writes candidates to that array
in the same left-to-right order it walked the spine. So the dict's
iteration order already *is* document order — no separate "order" field to
invent or keep in sync.

The output is deliberately generic (``assembly/main.md``, artifacts
concatenated with a heading per node) rather than LaTeX-specific — this
harness is corpus-agnostic (PLAN.md §1) and most corpora it runs over don't
compile at all. A caller that does need ``main.tex`` (or any other
compiled format) passes its own ``render`` callable; ``compile.py``'s
compile gate is likewise opt-in, not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..v0.run_dir import node_artifact_path
from ..v1.tree import TaskNode, TaskTree
from .run_dir import assembly_index_path, assembly_output_path

DEFAULT_HEADING_LEVEL = "##"


class AssemblyNotReadyError(RuntimeError):
    pass


@dataclass
class IndexEntry:
    node_id: str
    artifact: str


def ordered_node_ids(tree: TaskTree) -> list[str]:
    """Document order: the order nodes appear in ``tree.json`` (see module
    docstring for why that's already the right order and not a coincidence)."""
    return list(tree.nodes.keys())


def build_index(tree: TaskTree) -> list[IndexEntry]:
    return [
        IndexEntry(node_id=node_id, artifact=tree.nodes[node_id].artifact)
        for node_id in ordered_node_ids(tree)
    ]


def require_complete(tree: TaskTree) -> None:
    """Assembly reads every node's artifact, so every node must have
    actually passed first — §4.6 assumes the leaves are done, not that
    assembly is what finishes them. Raise with a full list, not just the
    first miss, since the caller needs to know how much is left."""
    incomplete = [n.id for n in tree.nodes.values() if n.status != "passed"]
    if incomplete:
        raise AssemblyNotReadyError(
            f"{len(incomplete)} node(s) not yet passed, cannot assemble: {incomplete}"
        )


def render_index_md(entries: list[IndexEntry]) -> str:
    lines = ["# Assembly index", ""]
    for i, entry in enumerate(entries, start=1):
        lines.append(f"{i}. `{entry.node_id}` — {entry.artifact}")
    return "\n".join(lines).rstrip() + "\n"


def _read_artifact(run_dir: Path, node_id: str) -> str:
    path = node_artifact_path(run_dir, node_id)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _default_render(node: TaskNode, text: str) -> str:
    return f"{DEFAULT_HEADING_LEVEL} {node.id}\n\n{text.strip()}\n"


def concatenate_artifacts(
    run_dir: str | Path,
    tree: TaskTree,
    *,
    render: Callable[[TaskNode, str], str] = _default_render,
) -> str:
    run_dir = Path(run_dir)
    parts = [
        render(tree.nodes[node_id], _read_artifact(run_dir, node_id))
        for node_id in ordered_node_ids(tree)
    ]
    return "\n\n".join(part.rstrip() for part in parts).rstrip() + "\n"


@dataclass
class AssemblyOutput:
    index_path: Path
    output_path: Path
    index: list[IndexEntry]
    text: str


def assemble(
    run_dir: str | Path,
    tree: TaskTree,
    *,
    filename: str = "main.md",
    render: Callable[[TaskNode, str], str] = _default_render,
    require_all_passed: bool = True,
) -> AssemblyOutput:
    """Write ``assembly/index.md`` and the concatenated output file. Zero
    model tokens — everything here is a script over already-passed
    artifacts and ``tree.json``."""
    if require_all_passed:
        require_complete(tree)

    entries = build_index(tree)
    index_text = render_index_md(entries)
    index_path = assembly_index_path(run_dir)
    index_path.write_text(index_text, encoding="utf-8")

    output_text = concatenate_artifacts(run_dir, tree, render=render)
    output_path = assembly_output_path(run_dir, filename)
    output_path.write_text(output_text, encoding="utf-8")

    return AssemblyOutput(
        index_path=index_path, output_path=output_path, index=entries, text=output_text
    )
