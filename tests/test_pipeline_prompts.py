"""Tests for pipeline/prompts.py's build_node_prompt.

No network, no provider — pure prompt assembly. Covers PLAN-zeromem.md §9's
feedback-carrying retry block: absent on a first attempt, patch-framed on a
retry, regenerate-framed once patching has already failed once; §11.2's
depends_on promotion injection; and §11.4's contract caching.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.pipeline.prompts import (  # noqa: E402
    _load_contract_cached,
    build_node_prompt,
)
from kusudaemon.v1.tree import TaskNode  # noqa: E402
from kusudaemon.v2.contract import ContractRule, freeze_contract, amend_contract  # noqa: E402
from kusudaemon.v2.retrieval import build_chunk_index  # noqa: E402
from kusudaemon.v2.survey import Chunk, SpineUnit, save_spine  # noqa: E402


def _node(**overrides) -> TaskNode:
    defaults: dict = dict(id="a", brief="Write the intro.", artifact="out/a.md", gates=["nonempty"])
    defaults.update(overrides)
    return TaskNode(**defaults)


class BuildNodePromptTest(unittest.TestCase):
    def test_includes_brief(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            prompt = build_node_prompt(_node(), run_dir)
        self.assertIn("Write the intro.", prompt)

    def test_first_attempt_prompt_has_no_defect_block(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            prompt = build_node_prompt(_node(), run_dir)
        self.assertNotIn("previous attempt", prompt.lower())

    def test_retry_prompt_includes_defect(self) -> None:
        node = _node(attempts=1, last_defect="nonempty: artifact is empty")
        with tempfile.TemporaryDirectory() as run_dir:
            prompt = build_node_prompt(node, run_dir)
        self.assertIn("nonempty: artifact is empty", prompt)

    def test_attempt_two_uses_patch_framing(self) -> None:
        node = _node(attempts=1, last_defect="nonempty: artifact is empty")
        with tempfile.TemporaryDirectory() as run_dir:
            prompt = build_node_prompt(node, run_dir)
        self.assertIn("MINIMAL", prompt)
        self.assertNotIn("Rewrite the artifact from scratch", prompt)

    def test_attempt_three_uses_regenerate_framing(self) -> None:
        node = _node(attempts=2, last_defect="nonempty: artifact is empty")
        with tempfile.TemporaryDirectory() as run_dir:
            prompt = build_node_prompt(node, run_dir)
        self.assertIn("Rewrite the artifact from scratch", prompt)
        self.assertNotIn("MINIMAL", prompt)


class DependsOnPromotionsTest(unittest.TestCase):
    """PLAN-zeromem.md §11.2: a node's depends_on promotions (the capped
    handoffs of the nodes it depends on) are injected; a node with no
    depends_on gets no such block; nothing is guessed from document order."""

    def _run_dir_with_manifest(self, root: Path) -> Path:
        run_dir = root / "run"
        run_dir.mkdir()
        (run_dir / "manifest.jsonl").write_text(
            json.dumps({"node": "upstream", "promotion": "delivered the intro and overview"}) + "\n"
            + json.dumps({"node": "other", "promotion": "unrelated section"}) + "\n",
            encoding="utf-8",
        )
        return run_dir

    def test_injects_promotions_of_depends_on(self) -> None:
        node = _node(id="b", depends_on=["upstream"])
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = self._run_dir_with_manifest(Path(root_str))
            prompt = build_node_prompt(node, run_dir)
        self.assertIn("delivered the intro and overview", prompt)
        self.assertNotIn("unrelated section", prompt)

    def test_no_depends_on_means_no_promotion_block(self) -> None:
        node = _node(id="a")
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = self._run_dir_with_manifest(Path(root_str))
            prompt = build_node_prompt(node, run_dir)
        self.assertNotIn("delivered the intro", prompt)
        self.assertNotIn("Unrelated", prompt)


class ContractCacheTest(unittest.TestCase):
    """PLAN-zeromem.md §11.4: contract.md is cached across build_node_prompt
    calls, and the cache invalidates when the contract actually changes."""

    def test_contract_cached_then_refreshed_on_amendment(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            freeze_contract(run_dir, [ContractRule(source="p1", shape="prose", text="rule one")])
            first = _load_contract_cached(run_dir)
            amend_contract(run_dir, "updated rule two", reason="test")
            second = _load_contract_cached(run_dir)
        self.assertEqual(first.strip().splitlines()[0], "# Contract")
        self.assertIn("updated rule two", second)
        self.assertNotEqual(first, second)

    def test_contract_cache_is_bounded(self) -> None:
        # §11.10.15: one entry per run dir, FIFO-evicted — a long-lived
        # process watching many runs must not accumulate forever.
        from kusudaemon.pipeline.prompts import _CONTRACT_CACHE_MAX, _contract_cache

        _contract_cache.clear()
        try:
            with tempfile.TemporaryDirectory() as root_str:
                root = Path(root_str)
                run_dirs = []
                for i in range(_CONTRACT_CACHE_MAX + 20):
                    run_dir = root / f"run{i}"
                    run_dir.mkdir()
                    freeze_contract(run_dir, [ContractRule(source="p1", shape="prose", text=f"rule {i}")])
                    run_dirs.append(run_dir)
                for run_dir in run_dirs:
                    text = _load_contract_cached(run_dir)
                    self.assertIn(f"rule", text)
            self.assertLessEqual(
                len(_contract_cache),
                _CONTRACT_CACHE_MAX,
                "contract cache must evict FIFO past its bound",
            )
        finally:
            _contract_cache.clear()


def _retrieval_run(root: Path) -> Path:
    """A run dir with a built chunk index + spine, so inline spans resolve."""
    run_dir = root / "run"
    run_dir.mkdir()
    chunks = [
        Chunk(index=i, text=text, tokens=len(text.split()))
        for i, text in enumerate(
            [
                "python experts discuss the history of python",
                "python syntax evolved through several versions",
                "python semantics shaped by the community",
            ]
        )
    ]
    units = [SpineUnit(id="unit-01", label="Python", start_chunk=0, end_chunk=2, tokens=30)]
    build_chunk_index(run_dir, chunks, units)
    save_spine(run_dir, units)
    return run_dir


class InlineSpansTest(unittest.TestCase):
    """PLAN-zeromem.md §4.4: the opt-in inline-spans mode replaces the bare
    input-path list with retrieved spans (keeping v4 finding paths), falls
    back silently to today's rendering when the index is missing, and must
    leave the default output byte-for-byte unchanged."""

    def _assert_default_unmodified(self, node: TaskNode, run_dir: Path) -> None:
        expected = (
            "Your brief: Write the intro.\n\n"
            "Inputs (read them with your tools before writing, and cite them "
            "where relevant):\n- spine/unit-01.md"
        )
        self.assertEqual(build_node_prompt(node, run_dir), expected)

    def test_default_prompt_unchanged(self) -> None:
        node = _node(inputs=["spine/unit-01.md"])
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = _retrieval_run(Path(root_str))
            self._assert_default_unmodified(node, run_dir)

    def test_inline_spans_replaces_input_paths(self) -> None:
        node = _node(inputs=["spine/unit-01.md"])
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = _retrieval_run(Path(root_str))
            prompt = build_node_prompt(node, run_dir, inline_spans=True)
        self.assertNotIn("Inputs (read them with your tools", prompt)
        self.assertIn("Source material (retrieved spans", prompt)
        self.assertIn("[unit-01 \u00b7 chunk 0]", prompt)

    def test_inline_spans_keeps_research_finding_paths(self) -> None:
        node = _node(inputs=["spine/unit-01.md", "scratch/a/finding-web-1.md"])
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = _retrieval_run(Path(root_str))
            prompt = build_node_prompt(node, run_dir, inline_spans=True)
        self.assertIn("- scratch/a/finding-web-1.md", prompt)
        self.assertIn("Source material (retrieved spans", prompt)

    def test_inline_spans_falls_back_when_index_missing(self) -> None:
        node = _node(inputs=["spine/unit-01.md"])
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str) / "run"
            run_dir.mkdir()
            prompt = build_node_prompt(node, run_dir, inline_spans=True)
        self.assertIn("Inputs (read them with your tools", prompt)
        self.assertIn("- spine/unit-01.md", prompt)
        self.assertNotIn("Source material", prompt)

    def test_inline_spans_includes_provenance_headers(self) -> None:
        node = _node(inputs=["spine/unit-01.md"])
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = _retrieval_run(Path(root_str))
            prompt = build_node_prompt(node, run_dir, inline_spans=True)
        self.assertEqual(prompt.count("[unit-01 \u00b7 chunk "), 3)


if __name__ == "__main__":
    unittest.main()
