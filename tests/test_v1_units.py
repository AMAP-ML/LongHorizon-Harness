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
from kusudaemon.v1.manifest import cap_promotion  # noqa: E402
from kusudaemon.v1.provider import (  # noqa: E402
    OpenAICompatibleProvider,
    ProviderError,
    ProviderHTTPError,
)
from kusudaemon.v1.tree import TaskTree, TreeValidationError  # noqa: E402
from kusudaemon.v1.writer import writer_prompt  # noqa: E402


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
    def test_writer_prompt_states_artifact_contract(self) -> None:
        prompt = writer_prompt("Write the intro.", Path("/tmp/run/scratch/a/promotion.json"))
        self.assertIn("your last message", prompt.lower())
        self.assertIn("Write the intro.", prompt)

    def test_writer_prompt_still_requests_promotion(self) -> None:
        promotion_path = Path("/tmp/run/scratch/a/promotion.json")
        prompt = writer_prompt("Write the intro.", promotion_path)
        self.assertIn(str(promotion_path), prompt)
        self.assertIn("promotion", prompt)


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

    def test_non_400_http_error_is_not_retried_without_format(self) -> None:
        # A 500 is a provider problem, not a response_format rejection —
        # it must propagate, not silently retry without the schema guard.
        def transport(url, payload, headers):
            raise ProviderHTTPError(500, "HTTP 500 from provider: boom")

        provider = OpenAICompatibleProvider(transport=transport, api_key="unused")
        with self.assertRaises(ProviderHTTPError) as ctx:
            provider.complete_json([{"role": "user", "content": "hi"}], self.SCHEMA)
        self.assertEqual(ctx.exception.status, 500)


if __name__ == "__main__":
    unittest.main()
