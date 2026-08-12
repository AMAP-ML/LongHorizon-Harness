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
    docstring for why that's already the right order and not a coincidence).

    PLAN.md §A8.3/§B5: a "split" node's own artifact is a *derived*
    concatenation of its already-grafted children (``v7/split.py``), and
    those children are themselves ordinary entries in ``tree.nodes`` in
    their own right. Including the split parent here too would duplicate
    its children's content in the top-level assembly — excluded, the same
    way a "split" status is excluded from being a defect elsewhere in this
    package (``check_all_nodes_passed``) without being excluded from
    *completeness* (``require_complete`` below, ``TaskTree.is_complete()``)."""
    return [node_id for node_id, node in tree.nodes.items() if node.status != "split"]


def build_index(tree: TaskTree) -> list[IndexEntry]:
    return [
        IndexEntry(node_id=node_id, artifact=tree.nodes[node_id].artifact)
        for node_id in ordered_node_ids(tree)
    ]


def require_complete(tree: TaskTree) -> None:
    """Assembly reads every node's artifact, so every node must have
    actually finished first — §4.6 assumes the leaves are done, not that
    assembly is what finishes them. Raise with a full list, not just the
    first miss, since the caller needs to know how much is left.

    "split" counts as finished (PLAN.md §A8/§B5): a split parent never
    reaches "passed" itself (its children do), but it is a genuine,
    successful terminal outcome, not an incomplete one — matching
    ``TaskTree.is_complete()``'s own treatment."""
    incomplete = [n.id for n in tree.nodes.values() if n.status not in ("passed", "split")]
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
    node_ids: list[str] | None = None,
) -> str:
    """``node_ids``, when given, overrides ``ordered_node_ids(tree)`` as the
    set/order to render — PLAN.md §A8.3: this is also how a split parent's
    own derived artifact gets built (``v7/split.py:maybe_derive_split_parent``,
    ``v3/checks.py:check_split_parents_derived``), scoped to just that
    parent's grafted children rather than the whole tree, without
    duplicating this join/render logic a second time."""
    run_dir = Path(run_dir)
    ids = node_ids if node_ids is not None else ordered_node_ids(tree)
    parts = [render(tree.nodes[node_id], _read_artifact(run_dir, node_id)) for node_id in ids]
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

    export_workspace_artifacts(run_dir)

    return AssemblyOutput(
        index_path=index_path, output_path=output_path, index=entries, text=output_text
    )


def export_workspace_artifacts(run_dir: str | Path) -> list[Path]:
    """Extract code blocks/files from node artifacts into the workspace directory."""
    import re
    run_dir = Path(run_dir)
    workspace_root = run_dir.parent.parent if (run_dir.parent.name == "runs" and run_dir.parent.parent.name == ".kusudaemon") else Path.cwd()
    if workspace_root.name == ".kusudaemon":
        workspace_root = workspace_root.parent

    exported: list[Path] = []
    out_dir = run_dir / "out"
    if not out_dir.is_dir():
        return exported

    header_re = re.compile(
        r"(?:^|\n)#+\s*[`'\"]?([\w\.\-\/]+\.[a-zA-Z0-9]+)[`'\"]?\s*\n+```[^\n]*\n(.*?)```",
        re.DOTALL,
    )
    for art_path in sorted(out_dir.glob("*.md")):
        content = art_path.read_text(encoding="utf-8", errors="replace")
        for match in header_re.finditer(content):
            fn, code = match.group(1).strip(), match.group(2)
            fname = Path(fn).name
            if fname and "." in fname and not fname.startswith("."):
                dest = workspace_root / fname
                dest.write_text(code if code.endswith("\n") else code + "\n", encoding="utf-8")
                exported.append(dest)
    return exported

