"""§C1 node-type template system tests (PLAN.md §C1).

Covers the registry resolver, the additive application rules, the
warn-vs-hard separation, the ``terms_defined`` absolute-path rewrite, and
the run-dir glossary write. The five new gates themselves are covered in
``test_v1_gates_c1.py``; the "warn gates never block a node" round-loop
integration is in ``test_v1_round_loop.py``; the driver's plan-phase
glossary wiring is in ``test_driver_phases.py``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.v1.tree import TaskNode, TaskTree  # noqa: E402
from kusudaemon.v6.templates import (  # noqa: E402
    NodeTemplate,
    apply_template_to_node,
    builtin_templates,
    glossary_for_tree,
    merge_template_into_tree,
    template_for,
    write_tree_glossary,
)


def _node(node_id: str, **overrides) -> TaskNode:
    base = dict(
        id=node_id,
        brief=f"do {node_id}",
        artifact=f"out/{node_id}.md",
        gates=["nonempty", "max_tokens:24000"],
    )
    base.update(overrides)
    return TaskNode(**base)


class TemplateResolverTest(unittest.TestCase):
    def test_shape_routing_for_all_four_builtins(self) -> None:
        self.assertEqual(template_for("problem-set-dominant").name, "problem-set")
        self.assertEqual(template_for("derivation-dominant").name, "derivation")
        self.assertEqual(template_for("reference-dominant").name, "reference")
        self.assertEqual(template_for("prose-dominant").name, "prose")

    def test_unknown_shape_falls_back_to_generic(self) -> None:
        template = template_for("no-such-shape")
        self.assertEqual(template.name, "generic")
        self.assertEqual(template.gates, ())
        self.assertEqual(template.warn_gates, ())
        self.assertEqual(template.judgment, ())

    def test_type_matching_supported_even_though_unused_by_builtins(self) -> None:
        # ``template_for`` resolves first-match-wins over the registry
        # tuple, so a custom template must precede the builtins to win a
        # shape the builtins claim — prepending is the test seam.
        custom = NodeTemplate(name="custom", types=("appendix",), warn_gates=("terms_defined",))
        import kusudaemon.v6.templates as templates_mod

        original = templates_mod._BUILTIN_TEMPLATES
        templates_mod._BUILTIN_TEMPLATES = (custom, *original)
        try:
            # An unknown shape + a matching type resolves through types.
            self.assertEqual(template_for("no-such-shape", "appendix").name, "custom")
            # A type that no earlier template claims falls through to the
            # builtin shape template.
            self.assertEqual(template_for("prose-dominant", "somenode").name, "prose")
        finally:
            templates_mod._BUILTIN_TEMPLATES = original


class ApplyTemplateTest(unittest.TestCase):
    def test_problem_set_template_lands_warn_gates_and_judgment(self) -> None:
        node = _node("p1", shape="problem-set-dominant")
        apply_template_to_node(node)
        # Hard gates are untouched: the template ships warn-only today.
        self.assertEqual(node.gates, ["nonempty", "max_tokens:24000"])
        self.assertIn("problems>=5", node.warn_gates)
        self.assertIn("headers:std", node.warn_gates)
        self.assertNotIn("problems>=5", node.gates)
        self.assertEqual(node.judgment, ["worked_examples_reachable"])
        self.assertIn("worked_examples_reachable", node.rubric)

    def test_generic_template_is_a_noop(self) -> None:
        node = _node("g1", shape="mystery-shape")
        before = (list(node.gates), list(node.warn_gates), list(node.judgment), dict(node.rubric))
        apply_template_to_node(node)
        self.assertEqual(list(node.gates), before[0])
        self.assertEqual(list(node.warn_gates), before[1])
        self.assertEqual(list(node.judgment), before[2])
        self.assertEqual(dict(node.rubric), before[3])

    def test_application_is_additive_existing_first(self) -> None:
        node = _node(
            "a1",
            shape="problem-set-dominant",
            gates=["contains:Summary"],
            warn_gates=["latex_balanced"],
            judgment=["custom_item"],
            rubric={"custom_item": "operator custom bar"},
        )
        apply_template_to_node(node)
        self.assertEqual(
            node.gates, ["contains:Summary"]
        )
        # Existing warn gate first, template's after.
        self.assertEqual(node.warn_gates, ["latex_balanced", "headers:std", "problems>=5"])
        # Judgment union, operator's item first; rubric: existing wins.
        self.assertEqual(node.judgment, ["custom_item", "worked_examples_reachable"])
        self.assertEqual(node.rubric["custom_item"], "operator custom bar")
        self.assertIn("worked_examples_reachable", node.rubric)

    def test_apply_idempotent(self) -> None:
        node = _node("p1", shape="problem-set-dominant")
        apply_template_to_node(node)
        apply_template_to_node(node)
        self.assertEqual(node.warn_gates, ["headers:std", "problems>=5"])
        self.assertEqual(node.gates, ["nonempty", "max_tokens:24000"])
        self.assertEqual(node.judgment, ["worked_examples_reachable"])

    def test_explicit_template_param_wins_over_resolution(self) -> None:
        node = _node("p1", shape="prose-dominant")
        apply_template_to_node(node, template=builtin_templates()[0])
        self.assertEqual(node.warn_gates, ["headers:std", "problems>=5"])

    def test_merge_template_into_tree_covers_every_node(self) -> None:
        tree = TaskTree(
            nodes={
                n.id: n
                for n in [
                    _node("c1", shape="problem-set-dominant"),
                    _node("c2", shape="derivation-dominant"),
                    _node("c3", shape="custom-shape"),
                ]
            }
        )
        merge_template_into_tree(tree)
        self.assertIn("problems>=5", tree.nodes["c1"].warn_gates)
        self.assertIn("latex_balanced", tree.nodes["c2"].warn_gates)
        self.assertEqual(tree.nodes["c3"].warn_gates, [])


class GlossaryWiringTest(unittest.TestCase):
    def test_terms_defined_gate_rewritten_to_absolute_path(self) -> None:
        node = _node("r1", shape="reference-dominant")
        apply_template_to_node(node)
        self.assertIn("terms_defined", node.warn_gates)
        self.assertNotIn(":", node.warn_gates[node.warn_gates.index("terms_defined")])

        apply_template_to_node(node, glossary_path=Path("/tmp/x/glossary.json"))
        self.assertIn("terms_defined:/tmp/x/glossary.json", node.warn_gates)
        # Other warn gates untouched by the rewrite.
        self.assertIn("headers:std", node.warn_gates)

    def test_glossary_for_tree_unions_template_glossaries(self) -> None:
        tree = TaskTree(nodes={"g1": _node("g1", shape="generic-shape")})
        # Builtin templates carry no glossary content, so the union is empty.
        self.assertEqual(glossary_for_tree(tree), {})

        custom = NodeTemplate(name="ref", shapes=("reference-dominant",), glossary={"term1": "loc1"})
        import kusudaemon.v6.templates as templates_mod

        original = templates_mod._BUILTIN_TEMPLATES
        templates_mod._BUILTIN_TEMPLATES = (custom, *original)
        try:
            tree = TaskTree(
                nodes={
                    "r1": _node("r1", shape="reference-dominant"),
                    "p1": _node("p1", shape="problem-set-dominant"),
                }
            )
            self.assertEqual(glossary_for_tree(tree), {"term1": "loc1"})
        finally:
            templates_mod._BUILTIN_TEMPLATES = original

    def test_write_tree_glossary_writes_only_when_content_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            empty_tree = TaskTree(nodes={"g1": _node("g1")})
            self.assertFalse(write_tree_glossary(run_dir, empty_tree))
            self.assertFalse((run_dir / "glossary.json").exists())

            custom = NodeTemplate(
                name="ref", shapes=("reference-dominant",), glossary={"term1": "loc1"}
            )
            import kusudaemon.v6.templates as templates_mod

            original = templates_mod._BUILTIN_TEMPLATES
            templates_mod._BUILTIN_TEMPLATES = (custom, *original)
            try:
                tree = TaskTree(nodes={"r1": _node("r1", shape="reference-dominant")})
                self.assertTrue(write_tree_glossary(run_dir, tree))
                data = json.loads((run_dir / "glossary.json").read_text(encoding="utf-8"))
                self.assertEqual(data, {"term1": "loc1"})
                # Write-once: a second call does not clobber.
                self.assertFalse(write_tree_glossary(run_dir, tree))
            finally:
                templates_mod._BUILTIN_TEMPLATES = original

    def test_glossary_path_helper_is_a_pure_getter(self) -> None:
        from kusudaemon.v0.run_dir import glossary_path

        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str) / "run"
            self.assertEqual(glossary_path(run_dir), run_dir / "glossary.json")
            self.assertFalse(glossary_path(run_dir).exists())
            # Getter must not create the directory either.
            self.assertFalse(run_dir.exists())


if __name__ == "__main__":
    unittest.main()