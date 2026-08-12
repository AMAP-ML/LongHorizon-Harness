"""Unit tests for v1's non-networked pieces: gates, tree validation, the
manifest promotion cap, and provider.py's structured-output fallback loop
(against an injected fake transport — no real HTTP, no API key).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.v1.gates import estimate_tokens, evaluate_gates  # noqa: E402
from kusudaemon.v1.manifest import append_manifest_line, cap_promotion, read_all_manifest_entries  # noqa: E402
from kusudaemon.v1.provider import (  # noqa: E402
    OpenAICompatibleProvider,
    ProviderError,
    ProviderHTTPError,
    RATE_LIMIT_BACKOFFS,
)
from kusudaemon.v1.reviewer import cap_artifact_text, review_node  # noqa: E402
from kusudaemon.v1.tree import TaskNode, TaskTree, TreeValidationError  # noqa: E402
from kusudaemon.v1.writer import run_writer_node, writer_prompt  # noqa: E402

sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))
from fake_provider import FakeProvider  # noqa: E402


class GatesTest(unittest.TestCase):
    def test_nonempty(self) -> None:
        results = evaluate_gates(["nonempty"], "  ")
        self.assertFalse(results[0].passed)
        results = evaluate_gates(["nonempty"], "hello")
        self.assertTrue(results[0].passed)

    def test_len_range(self) -> None:
        text = " ".join(["word"] * 10)
        self.assertTrue(evaluate_gates(["len:5-15"], text)[0].passed)
        self.assertFalse(evaluate_gates(["len:20-30"], text)[0].passed)

    def test_max_tokens(self) -> None:
        short = "a b c"
        long_text = " ".join(["word"] * 1000)
        self.assertTrue(evaluate_gates(["max_tokens:400"], short)[0].passed)
        self.assertFalse(evaluate_gates(["max_tokens:10"], long_text)[0].passed)

    def test_contains(self) -> None:
        self.assertTrue(evaluate_gates(["contains:## Overview"], "## Overview\ntext")[0].passed)
        self.assertFalse(evaluate_gates(["contains:## Overview"], "no headers here")[0].passed)

    def test_unknown_gate_fails_closed(self) -> None:
        result = evaluate_gates(["not_a_real_gate"], "anything")[0]
        self.assertFalse(result.passed)


class TreeValidationTest(unittest.TestCase):
    def _write(self, root: Path, nodes: list[dict]) -> Path:
        import json

        path = root / "tree.json"
        path.write_text(json.dumps(nodes), encoding="utf-8")
        return path

    def test_node_without_gates_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            path = self._write(
                root,
                [{"id": "a", "brief": "x", "artifact": "out/a.md", "gates": []}],
            )
            with self.assertRaises(TreeValidationError):
                TaskTree.load(path)

    def test_artifact_disagreeing_with_id_is_rejected(self) -> None:
        # PLAN.md §D0: node.artifact used to be decorative -- every real
        # reader derived out/<id>.md from node.id independently, so a
        # disagreeing node.artifact silently pointed a Writer's prompt at a
        # file nothing else ever reads or writes. Now the single source of
        # truth, asserted at load.
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            path = self._write(
                root,
                [{"id": "a", "brief": "x", "artifact": "out/wrong.md", "gates": ["nonempty"]}],
            )
            with self.assertRaises(TreeValidationError):
                TaskTree.load(path)

    def test_unknown_dependency_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            path = self._write(
                root,
                [
                    {
                        "id": "a",
                        "brief": "x",
                        "artifact": "out/a.md",
                        "gates": ["nonempty"],
                        "depends_on": ["does-not-exist"],
                    }
                ],
            )
            with self.assertRaises(TreeValidationError):
                TaskTree.load(path)

    def test_dependency_ordering_gates_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            path = self._write(
                root,
                [
                    {"id": "a", "brief": "x", "artifact": "out/a.md", "gates": ["nonempty"]},
                    {
                        "id": "b",
                        "brief": "y",
                        "artifact": "out/b.md",
                        "gates": ["nonempty"],
                        "depends_on": ["a"],
                    },
                ],
            )
            tree = TaskTree.load(path)
            self.assertEqual(tree.ready_nodes(), ["a"])
            tree.nodes["a"].status = "passed"
            self.assertEqual(tree.ready_nodes(), ["b"])
            self.assertFalse(tree.is_complete())
            tree.nodes["b"].status = "passed"
            self.assertTrue(tree.is_complete())

    def test_blocked_when_nothing_ready_and_nothing_in_flight(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            path = self._write(
                root, [{"id": "a", "brief": "x", "artifact": "out/a.md", "gates": ["nonempty"]}]
            )
            tree = TaskTree.load(path)
            tree.nodes["a"].status = "blocked"
            self.assertTrue(tree.is_blocked())

    def test_defect_survives_tree_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            path = self._write(
                root, [{"id": "a", "brief": "x", "artifact": "out/a.md", "gates": ["nonempty"]}]
            )
            tree = TaskTree.load(path)
            self.assertEqual(tree.nodes["a"].last_defect, "")
            tree.nodes["a"].last_defect = "nonempty: artifact is empty"
            tree.save(path)
            reloaded = TaskTree.load(path)
            self.assertEqual(reloaded.nodes["a"].last_defect, "nonempty: artifact is empty")

    # §11.7: node dicts and depends_on must fail with the TreeValidationError
    # contract, not a bare KeyError / silent infinite unreadiness.

    def test_missing_id_raises_tree_validation_error_not_keyerror(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            path = self._write(
                Path(root_str), [{"brief": "x", "artifact": "out/a.md", "gates": ["nonempty"]}]
            )
            with self.assertRaises(TreeValidationError):
                TaskTree.load(path)

    def test_depends_on_cycle_is_detected_at_load(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            path = self._write(
                Path(root_str),
                [
                    {
                        "id": "a",
                        "brief": "x",
                        "artifact": "out/a.md",
                        "gates": ["nonempty"],
                        "depends_on": ["b"],
                    },
                    {
                        "id": "b",
                        "brief": "y",
                        "artifact": "out/b.md",
                        "gates": ["nonempty"],
                        "depends_on": ["a"],
                    },
                ],
            )
            with self.assertRaisesRegex(TreeValidationError, "cycle"):
                TaskTree.load(path)

    def test_long_cycle_is_detected_not_just_two_node_loops(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            path = self._write(
                Path(root_str),
                [
                    {
                        "id": f"n{i}",
                        "brief": str(i),
                        "artifact": f"out/n{i}.md",
                        "gates": ["nonempty"],
                        "depends_on": [f"n{(i + 1) % 5}"],
                    }
                    for i in range(5)
                ],
            )
            with self.assertRaisesRegex(TreeValidationError, "cycle"):
                TaskTree.load(path)

    def test_split_is_a_valid_status(self) -> None:
        # PLAN.md §A8/§B5: a split parent's status, purely additive.
        with tempfile.TemporaryDirectory() as root_str:
            path = self._write(
                Path(root_str),
                [{"id": "big", "brief": "x", "artifact": "out/big.md", "gates": ["nonempty"], "status": "split"}],
            )
            tree = TaskTree.load(path)
            self.assertEqual(tree.nodes["big"].status, "split")

    def test_unknown_status_is_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            path = self._write(
                Path(root_str),
                [{"id": "a", "brief": "x", "artifact": "out/a.md", "gates": ["nonempty"], "status": "bogus"}],
            )
            with self.assertRaises(TreeValidationError):
                TaskTree.load(path)

    def test_parent_field_is_additive_and_defaults_empty(self) -> None:
        node = TaskNode(id="a", brief="x", artifact="out/a.md", gates=["nonempty"])
        self.assertEqual(node.parent, "")

    def test_parent_round_trips_and_dangling_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            path = self._write(
                Path(root_str),
                [
                    {"id": "big", "brief": "x", "artifact": "out/big.md", "gates": ["nonempty"], "status": "split"},
                    {
                        "id": "big.a", "brief": "y", "artifact": "out/big.a.md",
                        "gates": ["nonempty"], "parent": "big",
                    },
                ],
            )
            tree = TaskTree.load(path)
            self.assertEqual(tree.nodes["big.a"].parent, "big")

        with tempfile.TemporaryDirectory() as root_str:
            path = self._write(
                Path(root_str),
                [
                    {
                        "id": "orphan", "brief": "y", "artifact": "out/orphan.md",
                        "gates": ["nonempty"], "parent": "does-not-exist",
                    },
                ],
            )
            with self.assertRaises(TreeValidationError):
                TaskTree.load(path)

    def test_a_pre_existing_tree_json_without_parent_or_split_loads_unchanged(self) -> None:
        # CLAUDE.md's own rule for every additive field: an old tree.json
        # with no "parent" key and no "split" status anywhere loads exactly
        # as it always did.
        with tempfile.TemporaryDirectory() as root_str:
            path = self._write(
                Path(root_str), [{"id": "a", "brief": "x", "artifact": "out/a.md", "gates": ["nonempty"]}]
            )
            tree = TaskTree.load(path)
            self.assertEqual(tree.nodes["a"].parent, "")

    def test_is_complete_treats_split_as_complete_equivalent(self) -> None:
        # PLAN.md §A8/§B5: a run whose only non-"passed" node is a split
        # parent must not read as stuck.
        with tempfile.TemporaryDirectory() as root_str:
            path = self._write(
                Path(root_str),
                [
                    {"id": "big", "brief": "x", "artifact": "out/big.md", "gates": ["nonempty"], "status": "split"},
                    {
                        "id": "big.a", "brief": "y", "artifact": "out/big.a.md",
                        "gates": ["nonempty"], "status": "passed", "parent": "big",
                    },
                ],
            )
            tree = TaskTree.load(path)
            self.assertTrue(tree.is_complete())
            self.assertFalse(tree.is_blocked())


class ManifestLineTest(unittest.TestCase):
    def test_empty_gate_results_are_fail_not_pass(self) -> None:
        """§11.2: a failed episode records [] gate_results — all([]) must
        not flip the manifest's gates field to 'pass' (that line feeds
        document review and checks.py's derived views)."""
        with tempfile.TemporaryDirectory() as root_str:
            manifest = Path(root_str) / "manifest.jsonl"
            append_manifest_line(
                manifest,
                node_id="ch_a",
                artifact_path="out/ch_a.md",
                artifact_text="",
                gate_results=[],
                promotion="",
            )
            entries = read_all_manifest_entries(manifest)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["gates"], "fail")

    def test_attempt_lines_are_deduped_to_the_terminal_line(self) -> None:
        """§11.10.6: dispatch appends one line per attempt; readers must see
        exactly one record per node — the terminal one — or a thrice-retried
        node contributes three rows to document review's index."""
        with tempfile.TemporaryDirectory() as root_str:
            manifest = Path(root_str) / "manifest.jsonl"
            first = append_manifest_line(
                manifest,
                node_id="ch_a",
                artifact_path="out/ch_a.md",
                artifact_text="attempt one draft",
                gate_results=[evaluate_gates(["nonempty"], "attempt one draft")[0]],
                promotion="attempt one handoff",
            )
            second = append_manifest_line(
                manifest,
                node_id="ch_a",
                artifact_path="out/ch_a.md",
                artifact_text="attempt two draft",
                gate_results=[evaluate_gates(["nonempty"], "attempt two draft")[0]],
                promotion="attempt two handoff",
            )
            entries = read_all_manifest_entries(manifest)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["promotion"], "attempt two handoff")
            self.assertNotEqual(entries[0]["ts"], first["ts"])
            self.assertEqual(entries[0]["ts"], second["ts"])


class PromotionCapTest(unittest.TestCase):
    def test_short_text_is_untouched(self) -> None:
        text = "a short handoff note"
        self.assertEqual(cap_promotion(text), text)

    def test_long_text_is_truncated_under_cap(self) -> None:
        text = " ".join(["word"] * 2000)
        capped = cap_promotion(text)
        self.assertLess(estimate_tokens(capped), estimate_tokens(text))
        self.assertIn("truncated", capped)


class WriterPromptTest(unittest.TestCase):
    def test_writer_prompt_does_not_claim_last_message_is_the_artifact(self) -> None:
        # PLAN.md §D0: "your last message becomes the artifact file verbatim"
        # was a leftover from the deleted Claude Code/Codex adapters and
        # fights gptme's save/patch tool-loop grain -- the artifact path
        # instruction now lives in pipeline/prompts.py:build_node_prompt
        # (see test_pipeline_prompts.py), not here.
        prompt = writer_prompt("Write the intro.", Path("/tmp/run/scratch/a/promotion.json"))
        self.assertNotIn("last message", prompt.lower())
        self.assertIn("Write the intro.", prompt)

    def test_writer_prompt_still_requests_promotion(self) -> None:
        promotion_path = Path("/tmp/run/scratch/a/promotion.json")
        prompt = writer_prompt("Write the intro.", promotion_path)
        self.assertIn(str(promotion_path), prompt)

    def test_writer_prompt_omits_split_hint_by_default(self) -> None:
        prompt = writer_prompt("Write the intro.", Path("/tmp/run/scratch/a/promotion.json"))
        self.assertNotIn("split.json", prompt)

    def test_writer_prompt_includes_split_hint_when_given_a_path(self) -> None:
        # PLAN.md §B5: "gains the split-proposal option only when the
        # node's inputs already exceed budget" -- writer_prompt itself just
        # renders whatever it's handed; the gating is _inputs_exceed_budget
        # (below), called from run_writer_node.
        split_path = Path("/tmp/run/scratch/a/split.json")
        prompt = writer_prompt(
            "Write the intro.", Path("/tmp/run/scratch/a/promotion.json"), split_hint_path=split_path
        )
        self.assertIn(str(split_path), prompt)
        self.assertIn("2-8 children", prompt)


class InputsExceedBudgetTest(unittest.TestCase):
    """PLAN.md §B5: the split hint appears in a real dispatched prompt only
    when the node's own resolved inputs already exceed its budget -- "a
    node that cannot legally split is never told it can." Exercised through
    ``run_writer_node`` end to end (the fake adapter records the prompt it
    was given) rather than by calling the private helper directly, so this
    proves the wiring, not just the predicate."""

    def test_split_hint_absent_when_inputs_fit_budget(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            (run_dir / "scratch" / "a").mkdir(parents=True)
            (run_dir / "out").mkdir()
            input_path = run_dir / "small.md"
            input_path.write_text("a few words of input text", encoding="utf-8")
            node = TaskNode(
                id="a", brief="x", artifact="out/a.md", gates=["nonempty"],
                inputs=["small.md"],
            )
            prompt = _captured_writer_prompt(run_dir, node)
            self.assertNotIn("split.json", prompt)

    def test_split_hint_present_when_inputs_exceed_budget(self) -> None:
        from kusudaemon.v1.tree import NodeBudget

        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            (run_dir / "scratch" / "a").mkdir(parents=True)
            (run_dir / "out").mkdir()
            input_path = run_dir / "big.md"
            input_path.write_text(" ".join(["word"] * 200), encoding="utf-8")
            node = TaskNode(
                id="a", brief="x", artifact="out/a.md", gates=["nonempty"],
                inputs=["big.md"], budget=NodeBudget(tokens=5, calls=15),
            )
            prompt = _captured_writer_prompt(run_dir, node)
            self.assertIn("split.json", prompt)
            self.assertIn(str(run_dir / "scratch" / "a" / "split.json"), prompt)


def _captured_writer_prompt(run_dir: Path, node: TaskNode) -> str:
    """Runs one Writer episode against a fake adapter that just records the
    prompt it was handed, and returns it -- the real path
    ``run_writer_node`` takes, not a direct call to a private helper."""
    import asyncio

    from kusudaemon.environment.local import LocalEnvironment
    from kusudaemon.types import EpisodeBudget, EpisodeResult

    class _RecordingAdapter:
        has_file_tools = True
        supports_session_resume = False

        def __init__(self) -> None:
            self.last_prompt = ""

        async def run_episode(self, prompt, env, budget, live_trajectory_path=None, **kwargs):
            self.last_prompt = prompt
            return EpisodeResult(status="done", actions_log="", duration_ms=1, metadata={})

    adapter = _RecordingAdapter()
    asyncio.run(
        run_writer_node(
            run_dir, node, "Write the intro.", adapter,
            LocalEnvironment(tmp_dir=str(run_dir / "tmp")), EpisodeBudget(),
        )
    )
    return adapter.last_prompt


class StalePromotionUnlinkTest(unittest.TestCase):
    """§11.9: a retry that ignores the promotion instruction must not
    inherit the previous attempt's handoff — attempt 1's text would land in
    this attempt's manifest line. The stale file must be unlinked before
    dispatch, exactly like trace.jsonl."""

    def test_stale_promotion_is_not_read_on_redispatch(self) -> None:
        import asyncio
        import json

        from unittest import mock

        from kusudaemon.environment.local import LocalEnvironment
        from kusudaemon.types import EpisodeBudget, EpisodeResult
        from kusudaemon.v0.run_dir import create_run_dir, node_scratch_dir
        from kusudaemon.v1.writer import run_writer_node

        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            node = TaskNode(id="n", brief="b", artifact="out/n.md", gates=["nonempty"])
            promotion_path = node_scratch_dir(run_dir, "n") / "promotion.json"
            promotion_path.parent.mkdir(parents=True, exist_ok=True)
            promotion_path.write_text(json.dumps({"promotion": "STALE-HANDOFF"}), encoding="utf-8")

            # A retry whose episode never writes a promotion (the agent
            # ignored the instruction again): with the stale file unlinked,
            # the harness falls back to the visible output (empty here).
            async def fake_run_node(run_dir, node_id, prompt, adapter, env, budget):
                return EpisodeResult(status="done", actions_log="", error=None, duration_ms=0, metadata={})

            env = LocalEnvironment()
            budget = EpisodeBudget(max_duration_seconds=30)

            async def scenario() -> None:
                with mock.patch("kusudaemon.v1.writer.run_node", fake_run_node):
                    result, promotion = await run_writer_node(
                        run_dir, node, "write it", None, env, budget
                    )
                self.assertEqual(result.status, "done")
                self.assertEqual(promotion, "", "stale handoff must not survive a redispatch")

            asyncio.run(scenario())


class ProviderStructuredOutputTest(unittest.TestCase):
    SCHEMA = {
        "type": "object",
        "required": ["action"],
        "properties": {"action": {"type": "string", "enum": ["go", "stop"]}},
    }

    def test_retries_after_malformed_json_then_succeeds(self) -> None:
        calls = []

        def transport(url, payload, headers):
            calls.append(payload)
            if len(calls) == 1:
                return {"choices": [{"message": {"content": "not json at all"}}]}
            return {"choices": [{"message": {"content": '{"action": "go"}'}}]}

        provider = OpenAICompatibleProvider(transport=transport, api_key="unused")
        result = provider.complete_json([{"role": "user", "content": "hi"}], self.SCHEMA)
        self.assertEqual(result, {"action": "go"})
        self.assertEqual(len(calls), 2)

    def test_retries_after_schema_violation_then_succeeds(self) -> None:
        calls = []

        def transport(url, payload, headers):
            calls.append(payload)
            if len(calls) == 1:
                return {"choices": [{"message": {"content": '{"action": "not-a-valid-enum"}'}}]}
            return {"choices": [{"message": {"content": '{"action": "stop"}'}}]}

        provider = OpenAICompatibleProvider(transport=transport, api_key="unused")
        result = provider.complete_json([{"role": "user", "content": "hi"}], self.SCHEMA)
        self.assertEqual(result, {"action": "stop"})
        self.assertEqual(len(calls), 2)

    def test_on_reasoning_receives_reasoning_content_alongside_the_json(self) -> None:
        # §12: reasoning arrives as reasoning_content alongside content --
        # complete_json used to discard it entirely, leaving callers like
        # survey's explorer pseudo-agent with nothing to surface as
        # "thinking". on_reasoning is the opt-in hook that lets a caller
        # capture it without changing complete_json's return value.
        def transport(url, payload, headers):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"action": "go"}',
                            "reasoning_content": "weighing go vs stop...",
                        }
                    }
                ]
            }

        captured: list[str] = []
        provider = OpenAICompatibleProvider(transport=transport, api_key="unused")
        result = provider.complete_json(
            [{"role": "user", "content": "hi"}], self.SCHEMA, on_reasoning=captured.append
        )
        self.assertEqual(result, {"action": "go"})
        self.assertEqual(captured, ["weighing go vs stop..."])

    def test_on_reasoning_is_not_called_when_the_endpoint_sends_none(self) -> None:
        def transport(url, payload, headers):
            return {"choices": [{"message": {"content": '{"action": "go"}'}}]}

        captured: list[str] = []
        provider = OpenAICompatibleProvider(transport=transport, api_key="unused")
        provider.complete_json([{"role": "user", "content": "hi"}], self.SCHEMA, on_reasoning=captured.append)
        self.assertEqual(captured, [])

    def test_gives_up_after_exhausting_retries(self) -> None:
        def transport(url, payload, headers):
            return {"choices": [{"message": {"content": "still not json"}}]}

        provider = OpenAICompatibleProvider(transport=transport, api_key="unused")
        with self.assertRaises(ProviderError):
            provider.complete_json([{"role": "user", "content": "hi"}], self.SCHEMA, retries=1)

    def test_strips_code_fences(self) -> None:
        def transport(url, payload, headers):
            return {"choices": [{"message": {"content": '```json\n{"action": "go"}\n```'}}]}

        provider = OpenAICompatibleProvider(transport=transport, api_key="unused")
        result = provider.complete_json([{"role": "user", "content": "hi"}], self.SCHEMA)
        self.assertEqual(result, {"action": "go"})

    def test_http_400_retries_without_response_format(self) -> None:
        # PLAN-zeromem.md §11.3: an endpoint that 400s on response_format /
        # strict must still reach the plain-messages fallback instead of
        # failing on the first attempt.
        calls = []

        def transport(url, payload, headers):
            calls.append(payload)
            if len(calls) == 1:
                raise ProviderHTTPError(400, "HTTP 400 from provider: response_format not supported")
            return {"choices": [{"message": {"content": '{"action": "go"}'}}]}

        provider = OpenAICompatibleProvider(transport=transport, api_key="unused")
        result = provider.complete_json([{"role": "user", "content": "hi"}], self.SCHEMA)
        self.assertEqual(result, {"action": "go"})
        self.assertEqual(len(calls), 2)
        self.assertIn("response_format", calls[0])
        self.assertNotIn("response_format", calls[1])

    # §11.10.2: an endpoint that 400s on response_format must pay for its
    # discovery once per complete_json call, not once per attempt.

    def test_400_fallback_is_latched_across_validate_reprompt_attempts(self) -> None:
        calls = []

        def transport(url, payload, headers):
            calls.append(payload)
            if "response_format" in payload:
                raise ProviderHTTPError(400, "HTTP 400 from provider: response_format not supported")
            if len(calls) == 2:
                return {"choices": [{"message": {"content": "not json at all"}}]}
            return {"choices": [{"message": {"content": '{"action": "go"}'}}]}

        provider = OpenAICompatibleProvider(transport=transport, api_key="unused")
        result = provider.complete_json([{"role": "user", "content": "hi"}], self.SCHEMA, retries=1)
        self.assertEqual(result, {"action": "go"})
        # 1 (400) + 1 (formatless fallback, bad JSON) + 1 (formatless
        # reprompt): the second attempt must not re-send response_format —
        # that would be a fourth call and a second 400 discovery.
        self.assertEqual(len(calls), 3)
        self.assertNotIn("response_format", calls[2])

    def test_non_400_http_error_is_not_retried_without_format(self) -> None:
        # A 500 is a provider problem, not a response_format rejection —
        # after §11.10.3's backoff retries are exhausted it must propagate
        # with its status, not silently retry without the schema guard.
        def transport(url, payload, headers):
            raise ProviderHTTPError(500, "HTTP 500 from provider: boom")

        provider = OpenAICompatibleProvider(
            transport=transport, api_key="unused", max_http_retries=1, sleep=lambda _: None
        )
        with self.assertRaises(ProviderHTTPError) as ctx:
            provider.complete_json([{"role": "user", "content": "hi"}], self.SCHEMA)
        self.assertEqual(ctx.exception.status, 500)

    # §11.10.3 / §D11: 429 takes the operator-specified ladder (1m → 5m →
    # 30m → 1h → 3h → 5h, then raise); 5xx keeps the short exponential
    # loop. Retry-After can only extend a rung, never shrink it; other
    # statuses and ladder exhaustion surface immediately (the task stops).

    def test_429_uses_the_ladder_and_retries_until_success(self) -> None:
        # Two 429s then a success: the ladder fires twice (rungs 0 and 1),
        # the third HTTP call succeeds. Sleeps match the first two rungs of
        # RATE_LIMIT_BACKOFFS with ±20% jitter.
        calls: list[float] = []

        def transport(url, payload, headers):
            calls.append(0.0)
            if len(calls) < 3:
                raise ProviderHTTPError(429, "HTTP 429 from provider: rate limit")
            return {"choices": [{"message": {"content": '{"action": "stop"}'}}]}

        sleeps: list[float] = []
        provider = OpenAICompatibleProvider(transport=transport, api_key="unused", sleep=sleeps.append)
        result = provider.complete_json([{"role": "user", "content": "hi"}], self.SCHEMA)
        self.assertEqual(result, {"action": "stop"})
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(sleeps), 2)
        for observed, rung in zip(sleeps, RATE_LIMIT_BACKOFFS[:2]):
            self.assertGreaterEqual(observed, rung * 0.8)
            self.assertLessEqual(observed, rung * 1.2)

    def test_429_retry_after_can_only_extend_a_rung_never_shrink_it(self) -> None:
        # §D11: an endpoint ``Retry-After`` of 0.1s is below the first rung
        # (60s), so the ladder still waits the rung — the schedule is a
        # floor, not a hint. A ``Retry-After`` larger than the current rung
        # extends it (the endpoint knows its reset); capped at the ladder
        # ceiling.
        def transport(url, payload, headers):
            raise ProviderHTTPError(429, "rate limit", retry_after=0.1)

        sleeps: list[float] = []
        provider = OpenAICompatibleProvider(transport=transport, api_key="unused", sleep=sleeps.append)
        with self.assertRaises(ProviderHTTPError) as ctx:
            provider.complete_json([{"role": "user", "content": "hi"}], self.SCHEMA)
        self.assertEqual(ctx.exception.status, 429)
        # Six rungs fired (seven HTTP attempts total: the 7th's 429 re-raises).
        self.assertEqual(len(sleeps), len(RATE_LIMIT_BACKOFFS))
        for observed, rung in zip(sleeps, RATE_LIMIT_BACKOFFS):
            self.assertGreaterEqual(observed, rung * 0.8)
            self.assertLessEqual(observed, rung * 1.2)

    def test_429_retry_after_above_a_rung_extends_it_up_to_the_ceiling(self) -> None:
        # On rung 1 (300s) an endpoint ``Retry-After`` of 1200s extends the
        # wait to 1200s (it knows its own cooldown); an absurd ``Retry-After``
        # is capped at the ladder's 5h ceiling.
        calls: list[float] = []

        def transport(url, payload, headers):
            calls.append(0.0)
            if len(calls) == 1:
                raise ProviderHTTPError(429, "rate limit")  # rung 0: 60s, no header
            if len(calls) == 2:
                raise ProviderHTTPError(429, "rate limit", retry_after=1200.0)  # extends rung 1
            if len(calls) == 3:
                raise ProviderHTTPError(429, "rate limit", retry_after=99_999.0)  # capped at 5h
            return {"choices": [{"message": {"content": '{"action": "stop"}'}}]}

        sleeps: list[float] = []
        provider = OpenAICompatibleProvider(transport=transport, api_key="unused", sleep=sleeps.append)
        result = provider.complete_json([{"role": "user", "content": "hi"}], self.SCHEMA)
        self.assertEqual(result, {"action": "stop"})
        self.assertEqual(len(sleeps), 3)
        self.assertGreaterEqual(sleeps[0], RATE_LIMIT_BACKOFFS[0] * 0.8)
        self.assertLessEqual(sleeps[0], RATE_LIMIT_BACKOFFS[0] * 1.2)
        # rung 1 extended by Retry-After 1200s (no jitter floor violation:
        # the extension replaces the rung value before jitter).
        self.assertGreaterEqual(sleeps[1], 1200.0 * 0.8)
        self.assertLessEqual(sleeps[1], 1200.0 * 1.2)
        # rung 2 capped at the ladder ceiling (18000s), not 99999s.
        self.assertGreaterEqual(sleeps[2], RATE_LIMIT_BACKOFFS[-1] * 0.8)
        self.assertLessEqual(sleeps[2], RATE_LIMIT_BACKOFFS[-1] * 1.2)

    def test_429_exhaustion_raises_and_stops_the_task(self) -> None:
        # Seven 429s in a row -> the 7th re-raises (attempt == 6 == len).
        calls: list[float] = []

        def transport(url, payload, headers):
            calls.append(0.0)
            raise ProviderHTTPError(429, "rate limit")

        provider = OpenAICompatibleProvider(transport=transport, api_key="unused", sleep=lambda _: None)
        with self.assertRaises(ProviderHTTPError) as ctx:
            provider.complete_json([{"role": "user", "content": "hi"}], self.SCHEMA)
        self.assertEqual(ctx.exception.status, 429)
        # 1 initial + 6 rung retries = 7 total HTTP attempts.
        self.assertEqual(len(calls), len(RATE_LIMIT_BACKOFFS) + 1)

    def test_on_backoff_fires_before_each_rung_with_attempt_and_delay(self) -> None:
        calls: list[float] = []
        events: list[tuple[int, float]] = []

        def transport(url, payload, headers):
            calls.append(0.0)
            if len(calls) < 3:
                raise ProviderHTTPError(429, "rate limit")
            return {"choices": [{"message": {"content": '{"action": "stop"}'}}]}

        provider = OpenAICompatibleProvider(
            transport=transport,
            api_key="unused",
            sleep=lambda _: None,
            on_backoff=lambda attempt, delay: events.append((attempt, delay)),
        )
        provider.complete_json([{"role": "user", "content": "hi"}], self.SCHEMA)
        # Two backoffs (rungs 0 and 1), 1-based attempt numbers.
        self.assertEqual([ev[0] for ev in events], [1, 2])
        for (attempt, delay), rung in zip(events, RATE_LIMIT_BACKOFFS[:2]):
            self.assertGreaterEqual(delay, rung)
            self.assertLessEqual(delay, RATE_LIMIT_BACKOFFS[-1])

    def test_529_exhaustion_raises_the_original_status(self) -> None:
        def transport(url, payload, headers):
            raise ProviderHTTPError(503, "HTTP 503 from provider: unavailable")

        provider = OpenAICompatibleProvider(
            transport=transport, api_key="unused", max_http_retries=2, sleep=lambda _: None
        )
        with self.assertRaises(ProviderHTTPError) as ctx:
            provider.complete_json([{"role": "user", "content": "hi"}], self.SCHEMA)
        self.assertEqual(ctx.exception.status, 503)

    def test_400_is_not_backed_off(self) -> None:
        def transport(url, payload, headers):
            raise ProviderHTTPError(400, "HTTP 400 from provider: bad request")

        provider = OpenAICompatibleProvider(
            transport=transport, api_key="unused", max_http_retries=9, sleep=lambda _: None
        )
        with self.assertRaises(ProviderHTTPError) as ctx:
            provider.complete_json([{"role": "user", "content": "hi"}], self.SCHEMA)
        self.assertEqual(ctx.exception.status, 400)


class ReviewerInputCapTest(unittest.TestCase):
    """§11.10.13: the Reviewer (and the re-validation reviewer, which shares
    this capper) must never receive an uncapped artifact — §8's "small
    outputs everywhere" applied to the input side of a one-shot call."""

    def _node_with_judgment(self) -> TaskNode:
        return TaskNode(
            id="a",
            brief="write",
            artifact="out/a.md",
            gates=["nonempty"],
            judgment=["clarity"],
            rubric={"clarity": "be clear"},
        )

    def test_oversized_artifact_is_truncated_and_marked(self) -> None:
        huge = " ".join(["word"] * 60_000)  # ~80k heuristic tokens
        provider = FakeProvider([{"items": [], "verdict": "pass"}])
        verdict = review_node(self._node_with_judgment(), huge, provider)
        self.assertEqual(verdict.verdict, "pass")
        # §D5: a `passed` verdict reached over a truncated artifact must say
        # so on the verdict itself, not just in the prompt the model saw --
        # the record is what a later reader (dashboard, audit) checks.
        self.assertTrue(verdict.truncated)
        user_content = provider.calls[0][0][1]["content"]
        self.assertIn("ARTIFACT TRUNCATED", user_content)
        sent_artifact = user_content.split("Artifact:\n", 1)[1]
        self.assertLessEqual(estimate_tokens(sent_artifact), 8_000 + 50)

    def test_small_artifact_passes_through_unmodified(self) -> None:
        small = "Short artifact, no truncation needed."
        provider = FakeProvider([{"items": [], "verdict": "pass"}])
        verdict = review_node(self._node_with_judgment(), small, provider)
        self.assertFalse(verdict.truncated)
        user_content = provider.calls[0][0][1]["content"]
        self.assertIn("Short artifact, no truncation needed.", user_content)
        self.assertNotIn("ARTIFACT TRUNCATED", user_content)

    def test_cap_artifact_text_marks_rather_than_silently_cuts(self) -> None:
        capped = cap_artifact_text(" ".join(["x"] * 100), ceiling_tokens=10)
        self.assertIn("ARTIFACT TRUNCATED", capped)
        self.assertIn("x x x x x x x", capped)  # ceiling*0.75 words kept
        self.assertNotIn("x x x x x x x x", capped)
        self.assertEqual(cap_artifact_text("short", ceiling_tokens=10), "short")
        self.assertEqual(cap_artifact_text("anything", ceiling_tokens=0), "")


if __name__ == "__main__":
    unittest.main()
