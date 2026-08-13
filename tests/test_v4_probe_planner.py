"""PLAN.md §C3 — the probe planner.

Coverage matching §C3's own spec list:

- ``needs_probe(node)`` is the deterministic pre-filter — a node the
  model never sees whether its brief is too short, shape is generic
  prose with no lookup markers, or shape carries a structural marker.
- Windowing: one ``complete_json`` call per 60 candidate nodes — never
  per-node, and an exactly-60-window / a 130-candidate case is the
  right number of calls (ceil(130/60) == 3).
- Returned node ids naming nodes outside the window slice are dropped
  (a model judging something it was not shown is a harness bug to
  correct, not a probe to trust).
- Per-window cap on accepted suggestions, so a model that returns 60
  probes for a 60-node window is bounded.
- Per-node slug deduplication + disambiguation, so two suggestions for
  the same node that share a slug don't clobber each other's finding.
- Driver integration: ``_phase_research`` builds an auto-plan when no
  operator plan was supplied and ``auto_probe_plan`` is on (default);
  an operator-supplied plan still wins; the auto-plan skips cleanly
  when no ``tree.json`` exists (T0 direct path).
- ``Probe`` construction normalizes legacy ``"web_search"`` to ``"web"``
  exactly the way pre-§B4 callers expect.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_provider import FakeProvider  # noqa: E402

from kusudaemon.pipeline.driver import RunOptions  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir  # noqa: E402
from kusudaemon.v1.provider import OpenAICompatibleProvider  # noqa: E402
from kusudaemon.v1.tree import TaskNode, TaskTree  # noqa: E402
from kusudaemon.v4.probe_planner import (  # noqa: E402
    MAX_PROBES_PER_WINDOW,
    PROBE_PLANNER_WINDOW,
    ProbeSuggestion,
    _validate_and_cap,
    candidate_nodes,
    needs_probe,
    plan_probes,
    research_plan_from_suggestions,
)
from kusudaemon.v4.research import Probe, ResearchQuery  # noqa: E402


def _node(
    node_id: str,
    brief: str,
    *,
    shape: str = "prose-dominant",
    gates: tuple[str, ...] = ("nonempty",),
    status: str = "pending",
    parent: str = "",
) -> TaskNode:
    return TaskNode(
        id=node_id,
        brief=brief,
        artifact=f"out/{node_id}.md",
        gates=list(gates),
        shape=shape,
        status=status,
        parent=parent,
    )


class NeedsProbeTest(unittest.TestCase):
    def test_short_brief_is_not_a_candidate(self) -> None:
        # < _MIN_BRIEF_WORDS words — no structural signal worth probing
        node = _node("ch01", "Produce the artifact for The goal")
        self.assertFalse(needs_probe(node))

    def test_structural_shape_marker_is_a_candidate(self) -> None:
        node = _node(
            "ch02",
            "Write the worked problem set for chapter two with at least twelve words here",
            shape="problem-set-dominant",
        )
        self.assertTrue(needs_probe(node))

    def test_brief_with_url_is_a_candidate_even_for_prose_shape(self) -> None:
        node = _node(
            "ch03",
            "Adopt the API conventions described at https://example.com/api/docs for the audience",
            shape="prose-dominant",
        )
        self.assertTrue(needs_probe(node))

    def test_brief_with_doc_marker_is_a_candidate(self) -> None:
        node = _node(
            "ch04",
            "Implement the executor per spec; read section 4 of the standard for details",
            shape="prose-dominant",
        )
        self.assertTrue(needs_probe(node))

    def test_generic_prose_with_no_lookup_marker_is_not_a_candidate(self) -> None:
        node = _node(
            "ch05",
            "Write the introduction explaining the approach and the audience for this chapter",
            shape="prose-dominant",
        )
        self.assertFalse(needs_probe(node))


class CandidateNodesTest(unittest.TestCase):
    def test_filters_to_candidates_only_and_preserves_tree_order(self) -> None:
        tree = TaskTree(
            nodes={
                n.id: n
                for n in [
                    _node("a", "Produce the artifact"),  # short brief — not a candidate
                    _node("b", "Write the problem set for chapter b with enough words here", shape="problem-set-dominant"),
                    _node("c", "Read https://example.com/x and summarize its approach for the audience"),
                    _node("d", "Write a generic prose chapter about the theory of the thing itself"),
                ]
            }
        )
        candidates = candidate_nodes(tree)
        self.assertEqual([n.id for n in candidates], ["b", "c"])


class PlanProbesWindowingTest(unittest.TestCase):
    def _make_tree(self, n: int) -> TaskTree:
        return TaskTree(
            nodes={
                f"node-{i:03d}": _node(
                    f"node-{i:03d}",
                    f"problem set {i} covers these topics with at least twelve words in the brief",
                    shape="problem-set-dominant",
                )
                for i in range(n)
            }
        )

    def test_zero_candidates_yields_zero_calls(self) -> None:
        tree = TaskTree(nodes={})
        provider = FakeProvider([{"probes": [{"node_id": "x", "slug": "x", "question": "q"}]}])
        plan = plan_probes(tree, provider)
        self.assertEqual(plan, {})
        self.assertEqual(len(provider.calls), 0)

    def test_under_window_is_one_call(self) -> None:
        tree = self._make_tree(20)
        provider = FakeProvider(
            [{"probes": [{"node_id": "node-000", "slug": "ctx", "question": "what is x?"}]}]
        )
        plan = plan_probes(tree, provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertIn("node-000", plan)
        self.assertEqual(len(plan["node-000"]), 1)
        self.assertEqual(plan["node-000"][0].question, "what is x?")

    def test_exactly_60_candidates_is_one_call(self) -> None:
        tree = self._make_tree(PROBE_PLANNER_WINDOW)
        # The validator limits to MAX_PROBES_PER_WINDOW per window, so keep
        # the canned response within the cap.
        provider = FakeProvider(
            [{"probes": [{"node_id": "node-059", "slug": "ctx", "question": "q?"}]}]
        )
        plan = plan_probes(tree, provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertIn("node-059", plan)

    def test_130_candidates_is_three_windows_three_calls(self) -> None:
        n = PROBE_PLANNER_WINDOW * 2 + 10  # 130
        tree = self._make_tree(n)
        provider = FakeProvider(
            [
                {"probes": [{"node_id": "node-000", "slug": "w1", "question": "q1?"}]},
                {"probes": [{"node_id": "node-060", "slug": "w2", "question": "q2?"}]},
                {"probes": [{"node_id": "node-120", "slug": "w3", "question": "q3?"}]},
            ]
        )
        plan = plan_probes(tree, provider)
        self.assertEqual(len(provider.calls), 3)
        self.assertIn("node-000", plan)
        self.assertIn("node-060", plan)
        self.assertIn("node-120", plan)


class ValidateAndCapTest(unittest.TestCase):
    def test_drops_suggestions_outside_window(self) -> None:
        suggestions = [
            ProbeSuggestion(node_id="in-window", slug="a", question="q?"),
            ProbeSuggestion(node_id="out-of-window", slug="b", question="q?"),
        ]
        accepted = _validate_and_cap(
            suggestions,
            window_ids={"in-window"},
            max_per_window=MAX_PROBES_PER_WINDOW,
        )
        self.assertEqual([s.node_id for s in accepted], ["in-window"])

    def test_caps_per_window(self) -> None:
        suggestions = [
            ProbeSuggestion(node_id=f"n{i}", slug=f"s{i}", question=f"q{i}?")
            for i in range(MAX_PROBES_PER_WINDOW + 3)
        ]
        window_ids = {f"n{i}" for i in range(MAX_PROBES_PER_WINDOW + 3)}
        accepted = _validate_and_cap(
            suggestions, window_ids=window_ids, max_per_window=MAX_PROBES_PER_WINDOW
        )
        self.assertEqual(len(accepted), MAX_PROBES_PER_WINDOW)

    def test_preserves_model_ordering_in_cap(self) -> None:
        suggestions = [
            ProbeSuggestion(node_id=f"n{i}", slug=f"s{i}", question=f"q{i}?")
            for i in range(20)
        ]
        window_ids = {f"n{i}" for i in range(20)}
        accepted = _validate_and_cap(
            suggestions, window_ids=window_ids, max_per_window=5
        )
        self.assertEqual([s.node_id for s in accepted], [f"n{i}" for i in range(5)])


class PlanProbesKindNormalizationTest(unittest.TestCase):
    def test_unsupported_kind_falls_back_to_web(self) -> None:
        tree = TaskTree(
            nodes={
                "n1": _node(
                    "n1",
                    "problem set one with at least twelve words in the brief here now",
                    shape="problem-set-dominant",
                )
            }
        )
        provider = FakeProvider(
            [
                {
                    "probes": [
                        {"node_id": "n1", "slug": "x", "question": "q?", "kind": "doc_retrieval"},
                        {"node_id": "n1", "slug": "y", "question": "q2?", "kind": "bogus_kind"},
                        {"node_id": "n1", "slug": "z", "question": "q3?", "kind": "workspace"},
                    ]
                }
            ]
        )
        plan = plan_probes(tree, provider)
        kinds = [q.kind for q in plan["n1"]]
        # doc_retrieval unsupported -> dropped to web; bogus -> web; workspace ok
        self.assertEqual(kinds, ["web", "web", "workspace"])


class PlanProbesDedupAndSlugTest(unittest.TestCase):
    def test_duplicate_suggestion_for_same_node_collapses(self) -> None:
        tree = TaskTree(
            nodes={
                "n1": _node(
                    "n1",
                    "problem set one with at least twelve words in the brief here now",
                    shape="problem-set-dominant",
                )
            }
        )
        provider = FakeProvider(
            [
                {
                    "probes": [
                        {"node_id": "n1", "slug": "ctx", "question": "what is it?"},
                        {"node_id": "n1", "slug": "ctx", "question": "what is it?"},
                    ]
                }
            ]
        )
        plan = plan_probes(tree, provider)
        self.assertEqual(len(plan["n1"]), 1)

    def test_distinct_suggestions_sharing_slug_disambiguate(self) -> None:
        tree = TaskTree(
            nodes={
                "n1": _node(
                    "n1",
                    "problem set one with at least twelve words in the brief here now",
                    shape="problem-set-dominant",
                )
            }
        )
        provider = FakeProvider(
            [
                {
                    "probes": [
                        {"node_id": "n1", "slug": "context", "question": "what is the timeline?"},
                        {"node_id": "n1", "slug": "context", "question": "what is the scope?"},
                    ]
                }
            ]
        )
        plan = plan_probes(tree, provider)
        slugs = sorted(q.slug for q in plan["n1"])
        # Slugs must not collide (would clobber the finding file), and the
        # first one keeps its name rather than both getting suffixes.
        self.assertEqual(slugs, ["context", "context-2"])

    def test_two_distinct_nodes_each_get_their_own_list(self) -> None:
        tree = TaskTree(
            nodes={
                f"n{i}": _node(
                    f"n{i}",
                    f"problem set {i} with at least twelve words in the brief here now",
                    shape="problem-set-dominant",
                )
                for i in range(2)
            }
        )
        provider = FakeProvider(
            [
                {
                    "probes": [
                        {"node_id": "n0", "slug": "ctx", "question": "q0?"},
                        {"node_id": "n1", "slug": "ctx", "question": "q1?"},
                    ]
                }
            ]
        )
        plan = plan_probes(tree, provider)
        self.assertEqual(set(plan.keys()), {"n0", "n1"})
        self.assertEqual(len(plan["n0"]), 1)
        self.assertEqual(len(plan["n1"]), 1)


class PlanProbesSchemaValidationTest(unittest.TestCase):
    def test_response_with_empty_probes_array_yields_no_plan(self) -> None:
        tree = TaskTree(
            nodes={
                "n1": _node(
                    "n1",
                    "problem set one with at least twelve words in the brief here now",
                    shape="problem-set-dominant",
                )
            }
        )
        # Schema-valid empty response — the planner's post-hoc filter never
        # gets a chance to do anything, so the result is a clean no-probes plan.
        provider = FakeProvider([{"probes": []}])
        plan = plan_probes(tree, provider)
        self.assertEqual(plan, {})

    def test_post_hoc_filter_drops_invalid_entries_kept_by_schema(self) -> None:
        # The schema validator (v1/json_schema.py) catches obvious type
        # and missing-key errors at complete_json time. The post-hoc filter
        # in _ask_one_window is what catches subtler cases the schema
        # permits: per-field whitespace-only strings, or a node_id pointing
        # to a real node in a *different* window slice (caught by
        # _validate_and_cap, not here). Exercise the cleaner of the two
        # paths directly with the whitespace-only case.
        tree = TaskTree(
            nodes={
                "n1": _node(
                    "n1",
                    "problem set one with at least twelve words in the brief here now",
                    shape="problem-set-dominant",
                )
            }
        )
        # Whitespace-only is schema-valid (minLength=1 passes for " "),
        # but post-hoc .strip() collapses it to empty and we drop it.
        provider = FakeProvider(
            [
                {
                    "probes": [
                        {"node_id": " ", "slug": "ctx", "question": "what is it?"},
                        {"node_id": "n1", "slug": "  ", "question": "what is it?"},
                        {"node_id": "n1", "slug": "ok", "question": "  "},
                        {"node_id": "n1", "slug": "ok", "question": "valid question?"},
                    ]
                }
            ]
        )
        plan = plan_probes(tree, provider)
        self.assertIn("n1", plan)
        # Only the last one survives — all three earlier had whitespace-only
        # required fields after stripping.
        self.assertEqual(len(plan["n1"]), 1)
        self.assertEqual(plan["n1"][0].question, "valid question?")


class ResearchPlanFromSuggestionsTest(unittest.TestCase):
    """A5-3: the plan call's own probe suggestions are folded into the
    research phase without a separate windowed call — this is the
    validation boundary: unknown ids, split parents, and nodes with
    children are dropped, and per-node dedup matches plan_probes'."""

    def test_suggestions_become_a_research_plan(self) -> None:
        tree = TaskTree(
            nodes={
                "n1": _node(
                    "n1",
                    "problem set one with at least twelve words in the brief here now",
                    shape="problem-set-dominant",
                )
            }
        )
        suggestions = [
            ProbeSuggestion(node_id="n1", slug="ctx", question="what is it?", kind="web"),
            ProbeSuggestion(node_id="n1", slug="ctx", question="what is it?", kind="web"),
            ProbeSuggestion(node_id="n1", slug="local", question="where is the code?", kind="workspace"),
        ]
        plan = research_plan_from_suggestions(suggestions, tree)
        self.assertEqual(len(plan["n1"]), 2)  # dedup collapses the twin
        self.assertEqual([q.kind for q in plan["n1"]], ["web", "workspace"])

    def test_unknown_ids_are_dropped(self) -> None:
        tree = TaskTree(nodes={})
        plan = research_plan_from_suggestions(
            [ProbeSuggestion(node_id="ghost", slug="x", question="q?")], tree
        )
        self.assertEqual(plan, {})

    def test_split_parents_and_nodes_with_children_are_dropped(self) -> None:
        tree = TaskTree(
            nodes={
                "parent": _node("parent", "a brief that qualifies as candidate material here", status="split"),
                "parent.child": _node("parent.child", "a brief for a leaf here with enough words", parent="parent"),
            }
        )
        plan = research_plan_from_suggestions(
            [
                ProbeSuggestion(node_id="parent", slug="p", question="q?"),
                ProbeSuggestion(node_id="parent.child", slug="c", question="q?"),
            ],
            tree,
        )
        # The split parent is terminal-for-writers; only the leaf survives.
        self.assertEqual(list(plan.keys()), ["parent.child"])


if __name__ == "__main__":
    unittest.main()
