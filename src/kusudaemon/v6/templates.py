"""§C1: the node-type template system (PLAN.md).

Every leaf today ships ``nonempty`` + ``max_tokens`` and an **empty
``judgment``** list, so ``review_node`` auto-passes without a model call
and reviewer pass rate is pinned at 1.0 (PLAN.md §C1's framing: "still the
highest-value gap"). The template system is the thing that decides *which*
gates and judgment items a node of a given ``shape`` / ``type`` should
carry — instead of every leaf looking identical, a ``problem-set-dominant``
leaf gets ``problems>=5`` plus a judgment bar about worked examples being
dereferenced from the chapter's exposition.

**Ship at warn severity first** (PLAN.md's own §C1 ordering): every new
gate the template registry emits goes onto ``node.warn_gates`` rather than
``node.gates``, so a "missing 5th problem" warning reaches the audit file
and the manifest without flipping a passing run into a failing one. The
caller (the planner's ``template_for`` resolver, below) is the one place
that decides this; a test or hand-authored tree may write ``gates`` to
truly enforce a gate, the same way it always could. The plan: watch what
the warn-severity gates actually fire on across real corpora, tighten the
predicate where it is wrong, and only then graduate the entry — same
"ship default-off, measure, then flip" rule (§III.5) every other major
addition since §B1 follows.

**The registry itself is in-repo, not in-DB.** Templates are
hand-authored Python dataclasses (one per (shape, type) pair we know
about today; ``generic`` is the default), so adding a new template is a
code review, not a migration. An unknown ``(shape, type)`` falls back
to the ``generic`` template — never raises — so a free-form goal that
the planner labels with a shape we have no template for still gets a node
that gates on ``nonempty`` and ``max_tokens`` and reviews against the
parts of the contract freeze that landed in its ``rubric`` (today's exact
behavior, in other words).

Interaction with §A4 (PLAN.md's own note): templates are also what let a
T2 run get a real rubric without a pilot — the template's
``judgment``/``rubric`` defaults are what populate a leaf's rubric when
the only contract available is the script-rendered spec.md one
(§A10). Without templates, T2 leaves carry no judgment items at all and
``review_node`` auto-passes free.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..v2.survey import SpineUnit  # noqa: F401 — re-exported for callers

Shape = Literal[
    "prose-dominant", "derivation-dominant", "problem-set-dominant", "reference-dominant"
]


@dataclass(frozen=True)
class NodeTemplate:
    """A reusable per-shape/-type envelope of gates and rubric text.

    ``gates`` are the hard gates — failing one blocks the node from
    reaching ``"passed"``. ``warn_gates`` are §C1's new "ship at warn
    severity first" gates: evaluated and recorded, never blocking.

    ``judgment`` is the list of judgment-item ids the reviewer will ask an
    opinion about; ``rubric`` is the matching ``{id: one-line imperative}``
    text, contract-substituted when the driver builds the prompt. §6/§7's
    "gates vs. judgment" split — gates never enter model context, judgment
    is the terse soft bar — is enforced here by structuring a template as
    two separate lists rather than one merged one.
    """

    name: str
    shapes: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    # Hard gates that block (today's ``nonempty``/``max_tokens:N``). A
    # template's ``gates`` is *additive*: the planner merges them onto a
    # leaf's existing ``gates`` list, never replacing it (see
    # ``apply_template`` below), so a node the planner already gave
    # ``max_tokens:24000`` keeps that gate even when its template adds
    # ``headers:std`` too.
    gates: tuple[str, ...] = ()
    # §C1 warn-only gates: evaluated, recorded, never block. "Ship at
    # warn severity first" — see module docstring.
    warn_gates: tuple[str, ...] = ()
    judgment: tuple[str, ...] = ()
    rubric: dict[str, str] = field(default_factory=dict)
    # The episode's tool allowlist for a node of this shape (PLAN-AUDIT-COST
    # §A6-3). Empty means "no opinion" — the adapter keeps its
    # ``DEFAULT_TOOL_ALLOWLIST`` fallback (T0/T1 direct nodes and
    # generic-shape leaves never carry a template opinion). gptme's system
    # prompt rebuilds per episode and its tool-doc blocks are the largest
    # stable prefix component, so a narrower allowlist is a real per-episode
    # token saving: a prose leaf needs ``read``/``save``, not ``shell``.
    tools: tuple[str, ...] = ()
    # Optional ``glossary.json`` content supplied as the contract-substituted
    # judgment text for templates whose rubric *is* a glossary up-front
    # (used by ``terms_defined``). The harness writes this to the run dir
    # once intake has stabilized; the warn-gate handler reads it back.
    glossary: dict[str, str] = field(default_factory=dict)


# -----------------------------------------------------------------------
# Builtin registry — hand-authored; extending it is a code review.
# -----------------------------------------------------------------------

# The ``generic`` template: a leaf whose shape we have no specific
# template for. Keeps today's exact behavior — only the gates the planner
# already emits, no judgment items — so an unrecognized shape doesn't
# suddenly require reviewed judgment it can't support. ``generic`` is the
# fallback for ``template_for(shape, type)`` when no specific template
# matches; see the resolver below.
_GENERIC = NodeTemplate(name="generic")

# A problem-set leaf (§6's example shape): ships ``problems>=5`` as a
# warn-gate (the §15.4 example), ``headers:std`` for a basic heading
# policy, and a single judgment item asking the reviewer to confirm each
# worked problem actually works through its stated method. The judgment
# text is contract-substitutable: when the driver freezes a contract whose
# own §4.4-derived rules say "use SI units," that rule becomes part of the
# judgment text rather than being elided — the template just names the
# slot for it. §A6-3: keeps ``shell`` (computing/checking answers is the
# point of a problem set) but drops ``patch``.
_PROBLEM_SET = NodeTemplate(
    name="problem-set",
    shapes=("problem-set-dominant",),
    warn_gates=("headers:std", "problems>=5"),
    judgment=("worked_examples_reachable",),
    tools=("read", "save", "shell"),
    rubric={
        "worked_examples_reachable": (
            "Each worked example reaches its stated answer following a "
            "method this chapter establishes; an answer without a "
            "reachable method is a defect."
        ),
    },
)

# A derivation-dominant leaf: the math notation has to be syntactically
# balanced before any reviewer can fairly judge whether the algebra is
# right — ``latex_balanced`` is the structural precondition for an
# operator-style judgment about derivation correctness. §A6-3: keeps
# ``shell`` (a derivation writer plausibly wants to verify a step
# computationally) but drops ``patch``.
_DERIVATION = NodeTemplate(
    name="derivation",
    shapes=("derivation-dominant",),
    warn_gates=("headers:std", "latex_balanced"),
    judgment=("derivation_self_consistent",),
    tools=("read", "save", "shell"),
    rubric={
        "derivation_self_consistent": (
            "Each step follows from the one before it; a derivation that "
            "skips a non-trivial step or asserts what it should prove is "
            "a defect."
        ),
    },
)

# A reference-dominant leaf — a glossary/appendix. ``terms_defined``
# checks every bold or bracket-quoted candidate term against the on-disk
# glossary.json the driver writes once the leaf is published. §A6-3: like
# prose, writing-only — ``read``/``save``, no ``shell``/``patch``.
_REFERENCE = NodeTemplate(
    name="reference",
    shapes=("reference-dominant",),
    warn_gates=("headers:std", "terms_defined", "refs_resolve"),
    judgment=("every_term_defined_once",),
    tools=("read", "save"),
    rubric={
        "every_term_defined_once": (
            "Every term in this reference is defined exactly once, on its "
            "first appearance; a re-defined term or an undefined "
            "dereference is a defect."
        ),
    },
)

# A prose-dominant leaf: ``headers:std`` for basic hygiene only. The
# reviewer is the one who notices the actual semantic gaps, so this
# template ships *no* judgment items — §B6's fan-out already ensures the
# reviewer sees the whole (over-cap-bounded) artifact, which is the real
# semantic bar for prose. Writing too many judgment items here is the
# monotonic-inflation failure §4.4 forbade for the contract, repeated at
# the leaf layer.
#
# §A6-3: ``tools=("read", "save")`` — a prose leaf writes one artifact from
# its inputs; it never executes anything, so gptme's ``shell`` (the largest
# tool-doc block) and ``patch`` (unneeded when ``save`` can rewrite the
# artifact wholesale) leave the episode prompt.
_PROSE = NodeTemplate(
    name="prose",
    shapes=("prose-dominant",),
    warn_gates=("headers:std",),
    tools=("read", "save"),
)

_BUILTIN_TEMPLATES: tuple[NodeTemplate, ...] = (
    _PROBLEM_SET,
    _DERIVATION,
    _REFERENCE,
    _PROSE,
    _GENERIC,
)


def builtin_templates() -> tuple[NodeTemplate, ...]:
    """Read-only view of the registry. Callers should not mutate; tests
    can re-derive a fresh tuple if they need to vary the set."""
    return _BUILTIN_TEMPLATES


def template_for(shape: str, node_type: str = "generic") -> NodeTemplate:
    """§C1's resolver: first-match-wins over the builtin registry, with
    ``generic`` as the always-safe fallback. ``shape`` is the planner's
    own ``_SHAPES`` field (e.g. ``prose-dominant``); ``node_type`` is
    ``TaskNode.type`` — today always ``"generic"`` since the planner
    doesn't yet emit richer types, but the resolver already supports a
    future where it does (templates carry a ``types`` tuple precisely so
    a richer type system can land without rewriting this function)."""
    # Specific ``(shape, type)`` templates would win first; today the
    # registry only keys on ``shape`` (every template's ``types`` is
    # empty), so the loop is effectively shape-only and ``generic`` is
    # both the default fallback and the fallback for an unknown shape.
    for template in _BUILTIN_TEMPLATES:
        if template.shapes and shape in template.shapes:
            return template
        if template.types and node_type in template.types:
            return template
    return _GENERIC


# -----------------------------------------------------------------------
# Application: write a template's gates / judgment / rubric onto a node.
# -----------------------------------------------------------------------


def apply_template_to_node(
    node,
    *,
    template: NodeTemplate | None = None,
    glossary_path: Path | None = None,
) -> None:
    """Merge ``template`` (default: resolved from the node's own
    ``shape``/``type``) onto the node, in place, additive.

    - ``gates``: union with the node's existing gates (de-duplicated,
      order preserved: existing gates first, new templates' gates after).
    - ``warn_gates``: union the same way. §C1's "ship at warn severity
      first" lives here — the template's warn gates go only onto
      ``warn_gates``, never ``gates``. When ``glossary_path`` is given, a
      ``terms_defined`` warn gate is rewritten to carry it as an absolute
      path arg (``terms_defined:/abs/path/glossary.json``) — the gate
      handler has no run_dir of its own, and the evaluator process's cwd
      is the operator's launch dir, not necessarily the run dir, so the
      *relative* ``glossary.json`` default would silently dereference a
      nonexistent file in the wrong place. The planner's bare call passes
      no path; the driver re-merges with ``glossary_path(run_dir)`` after
      ``build_tree`` so the stored tree.json carries the absolute form
      (see ``merge_template_into_tree``).
    - ``judgment``/``rubric``: union; existing entries win (the operator
      who froze a contract-derived rubric trumps a template default).
    - ``tools``: set only when the node carries none — an explicitly
      tooled node (a hand-authored tree, a split child that inherited its
      parent's set) keeps its own list. §A6-3's per-shape allowlists
      therefore reach planner-built leaves while T0/T1 direct nodes
      (no shape, ``generic`` template, empty tools) keep the adapter's
      ``DEFAULT_TOOL_ALLOWLIST`` fallback.
    - ``glossary``: the node carries it as a free field (no live use yet
      from this function — the driver writes the glossary to run_dir,
      not the node — but the template provides the content so the driver
      doesn't have to re-derive it). Kept on the template only.

    Pure-additive: a node with empty ``gates``/``warn_gates``/``judgment``
    after this is one whose template's ``template_for(shape, type)``
    resolved to ``generic`` and ``generic`` adds nothing — i.e. today's
    exact behavior, byte-for-byte. A caller who wants *only* template
    gates (no ``nonempty``/``max_tokens``) can post-process; this layer
    does not assume that's not what was intended.
    """
    if template is None:
        template = template_for(node.shape, node.type)
    # gates: union, existing first
    existing_gates = list(node.gates)
    for gate in template.gates:
        if gate not in existing_gates:
            existing_gates.append(gate)
    node.gates = existing_gates
    # warn_gates: union, existing first
    existing_warn = list(node.warn_gates)
    for gate in template.warn_gates:
        if gate not in existing_warn:
            existing_warn.append(gate)
    if glossary_path is not None:
        existing_warn = [
            (
                f"terms_defined:{glossary_path}"
                if gate.startswith("terms_defined") and ":" not in gate
                else gate
            )
            for gate in existing_warn
        ]
    node.warn_gates = existing_warn
    # judgment: union, existing first (operator-frozen contract trumps
    # template default).
    existing_judgment = list(node.judgment)
    for judgment_id in template.judgment:
        if judgment_id not in existing_judgment:
            existing_judgment.append(judgment_id)
    node.judgment = existing_judgment
    # rubric: dict update — existing wins.
    merged_rubric = dict(template.rubric)
    merged_rubric.update(node.rubric)
    node.rubric = merged_rubric
    # tools: the node's own list wins; a tool-less node takes the
    # template's per-shape allowlist (§A6-3).
    if not node.tools and template.tools:
        node.tools = list(template.tools)


def merge_template_into_tree(
    tree, *, glossary_path: Path | None = None
) -> None:
    """Convenience: apply ``template_for`` to every leaf in the tree
    in-place. A plan-time operation — once the tree is saved, each leaf
    carries its template's gates and judgment forever, so a resume
    doesn't re-apply (the additive merge is idempotent anyway, but the
    cost is zero on a tree that already carries the gates). The driver
    passes ``glossary_path=glossary_path(run_dir)`` so the ``terms_defined``
    warn gate stores the run dir's absolute glossary location (the
    planner's own per-leaf ``apply_template_to_node`` call cannot know
    run_dir)."""
    for node in tree.nodes.values():
        apply_template_to_node(node, glossary_path=glossary_path)


def glossary_for_tree(tree) -> dict[str, str]:
    """§C1: the union of every template glossary the tree's nodes resolve
    to (by ``shape``/``type``, via ``template_for``). Nodes themselves
    carry no glossary field — the content lives on the template it
    resolved from — so this re-resolves. Empty for any tree that resolved
    only to templates with no glossary content (every builtin except the
    reference template in a personal registry today)."""
    merged: dict[str, str] = {}
    for node in tree.nodes.values():
        template = template_for(node.shape, node.type)
        for term, definition in template.glossary.items():
            merged.setdefault(term, definition)
    return merged


def write_tree_glossary(run_dir: str | Path, tree) -> bool:
    """§C1: write the tree's template-glossary union to
    ``<run_dir>/glossary.json`` — the file the ``terms_defined`` warn-gate
    dereferences against. Write-once: an existing file is never clobbered
    (a resume must not overwrite terms a previous plan phase already
    committed), and an empty union writes nothing (v2's stated pattern of
    never leaving a misleading empty file behind). Returns whether a file
    was written. The builtin reference template ships an empty glossary,
    so today this is a no-op unless a personal registry supplies content —
    the machinery exists and is tested; the content source is a future
    intake contract."""

    glossary = glossary_for_tree(tree)
    if not glossary:
        return False
    from ..v0.run_dir import glossary_path, write_text_atomic

    path = glossary_path(run_dir)
    if path.exists():
        return False
    write_text_atomic(
        path, json.dumps(glossary, indent=2, sort_keys=True) + "\n"
    )
    return True
