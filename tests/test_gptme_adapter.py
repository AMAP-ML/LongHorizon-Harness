"""GptmeAdapter: a Writer backend with no agent CLI in the chain (adapters/
gptme_adapter.py, adapters/_gptme_worker.py). Like every other adapter's
own tests, these exercise command construction and output parsing only --
no real gptme install, no network, no API key. (Verified separately, by
hand, against a real `pip install gptme` in a scratch venv while building
this: exact function signatures, the "local/<name>" + OPENAI_BASE_URL/
OPENAI_API_KEY custom-endpoint mechanism, and the `--output-format json`
line shape `gptme_visible_output` parses below all came from inspecting
the real installed package, not from documentation alone.)

Coverage:
- API key resolution order (explicit arg > KUSUDAEMON_PROVIDER_API_KEY >
  OPENAI_API_KEY from the provider's api_key_env) and the loud failure
  when none are set
- env vars (OPENAI_BASE_URL/OPENAI_API_KEY/GPTME_CONTEXT_LENGTH) and the
  --tool-allowlist/--model/--tool-format flags reach the command line
- the worker script this adapter shells out to actually exists on disk
- gptme_visible_output correctly extracts the LAST assistant message from
  a multi-line --output-format json transcript, ignoring non-JSON lines,
  malformed JSON, and non-assistant roles
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.adapters._gptme_worker import _wrap_thinking_stream  # noqa: E402
from kusudaemon.adapters.gptme_adapter import (  # noqa: E402
    _WORKER_SCRIPT,
    GptmeAdapter,
    gptme_visible_output,
)

_OPCODE_MODEL = "local/deepseek-v4-flash-free"
# resolve() no longer has a built-in fallback (see provider_config.py) --
# tests that aren't specifically exercising base_url/model resolution set
# these via _EnvGuard so a missing/absent real provider.json in the repo
# root the tests happen to run from can't make resolve() raise.
_TEST_BASE_URL = "https://test.example.com/v1"
_TEST_MODEL = "opencode/deepseek-v4-flash-free"


class _EnvGuard:
    """Snapshots and restores the env vars GptmeAdapter reads, so tests
    never leak credentials into each other regardless of pass/fail. Also
    points KUSUDAEMON_PROVIDER_CONFIG at a nonexistent path so a real
    provider.json sitting in the repo root the tests run from can't leak
    into tests, and fills OPENAI_BASE_URL/OPENAI_MODEL with fixture values
    so resolve() (which now raises rather than falling back to a built-in
    default) has something to find regardless."""

    _KEYS = (
        "KUSUDAEMON_PROVIDER_BASE_URL",
        "KUSUDAEMON_PROVIDER_API_KEY",
        "KUSUDAEMON_PROVIDER_MODEL",
        "KUSUDAEMON_PROVIDER_CONFIG",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "KUSUDAEMON_PROVIDER",
    )

    def __enter__(self) -> "_EnvGuard":
        self._backup = {key: os.environ.pop(key, None) for key in self._KEYS}
        os.environ["KUSUDAEMON_PROVIDER_CONFIG"] = "/nonexistent/provider.json"
        os.environ["OPENAI_BASE_URL"] = _TEST_BASE_URL
        os.environ["OPENAI_MODEL"] = _TEST_MODEL
        return self

    def __exit__(self, *exc_info: object) -> None:
        for key, value in self._backup.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)


class WorkerScriptTest(unittest.TestCase):
    def test_worker_script_exists(self) -> None:
        self.assertTrue(_WORKER_SCRIPT.is_file())


class ApiKeyResolutionTest(unittest.TestCase):
    def test_missing_key_raises(self) -> None:
        with _EnvGuard():
            with self.assertRaises(ValueError):
                GptmeAdapter()

    def test_explicit_key_wins_over_env(self) -> None:
        with _EnvGuard():
            os.environ["OPENAI_API_KEY"] = "env-key"
            adapter = GptmeAdapter(api_key="explicit-key")
            self.assertIn("explicit-key", adapter.command_template)
            self.assertNotIn("env-key", adapter.command_template)

    def test_provider_env_key_wins_over_generic(self) -> None:
        with _EnvGuard():
            os.environ["KUSUDAEMON_PROVIDER_API_KEY"] = "provider-key"
            os.environ["OPENAI_API_KEY"] = "generic-key"
            adapter = GptmeAdapter()
            self.assertIn("provider-key", adapter.command_template)
            self.assertNotIn("generic-key", adapter.command_template)

    def test_generic_key_used_when_provider_key_unset(self) -> None:
        with _EnvGuard():
            os.environ["OPENAI_API_KEY"] = "generic-key"
            adapter = GptmeAdapter()
            self.assertIn("generic-key", adapter.command_template)


class CommandConstructionTest(unittest.TestCase):
    def test_default_model_and_base_url(self) -> None:
        with _EnvGuard():
            adapter = GptmeAdapter(api_key="k")
        self.assertIn(_OPCODE_MODEL, adapter.command_template)
        self.assertIn(_TEST_BASE_URL, adapter.command_template)
        self.assertIn("OPENAI_BASE_URL=", adapter.command_template)
        self.assertIn("OPENAI_API_KEY=", adapter.command_template)

    def test_custom_model_and_base_url_override_defaults(self) -> None:
        with _EnvGuard():
            adapter = GptmeAdapter(api_key="k", model="local/custom", base_url="https://example.com/v1")
        self.assertIn("local/custom", adapter.command_template)
        self.assertIn("https://example.com/v1", adapter.command_template)
        self.assertNotIn(_TEST_BASE_URL, adapter.command_template)

    def test_tool_allowlist_reaches_command_line(self) -> None:
        with _EnvGuard():
            adapter = GptmeAdapter(api_key="k", tool_allowlist=("shell", "read"))
        self.assertIn("--tool-allowlist", adapter.command_template)
        self.assertIn("shell,read", adapter.command_template)

    def test_context_length_omitted_by_default(self) -> None:
        with _EnvGuard():
            adapter = GptmeAdapter(api_key="k")
        self.assertNotIn("GPTME_CONTEXT_LENGTH", adapter.command_template)

    def test_context_length_set_when_given(self) -> None:
        with _EnvGuard():
            adapter = GptmeAdapter(api_key="k", context_length=128_000)
        self.assertIn("GPTME_CONTEXT_LENGTH=128000", adapter.command_template)

    def test_no_session_resume_support(self) -> None:
        self.assertFalse(GptmeAdapter.supports_session_resume)
        self.assertTrue(GptmeAdapter.supports_tool_restriction)


class GptmeVisibleOutputTest(unittest.TestCase):
    def test_extracts_last_assistant_message(self) -> None:
        raw = "\n".join(
            [
                '{"type": "message", "role": "user", "content": "hi"}',
                '{"type": "message", "role": "assistant", "content": "first reply"}',
                '{"type": "message", "role": "assistant", "content": "final reply"}',
            ]
        )
        self.assertEqual(gptme_visible_output(raw), "final reply")

    def test_ignores_non_json_and_malformed_lines(self) -> None:
        raw = "\n".join(
            [
                "Using logdir: /tmp/foo",
                "{not valid json",
                '{"type": "message", "role": "assistant", "content": "the answer"}',
            ]
        )
        self.assertEqual(gptme_visible_output(raw), "the answer")

    def test_no_assistant_messages_returns_empty(self) -> None:
        raw = '{"type": "message", "role": "user", "content": "hi"}'
        self.assertEqual(gptme_visible_output(raw), "")

    def test_ignores_non_message_events(self) -> None:
        raw = "\n".join(
            [
                '{"type": "other", "role": "assistant", "content": "ignored"}',
                '{"type": "message", "role": "assistant", "content": "counted"}',
            ]
        )
        self.assertEqual(gptme_visible_output(raw), "counted")


class ThinkingStreamWrapperTest(unittest.TestCase):
    """PLAN-AUDIT.md §E19/§F3/§E20l: `_wrap_thinking_stream` wraps gptme's
    `gptme.llm._stream` to intercept `<think>` spans for live thinking
    display. Exercised directly against a fake `_StreamWithMetadata`-shaped
    object (a plain object with a `.gen` generator attribute, matching the
    real gptme 0.32.1 shape confirmed by hand) -- no real gptme install
    involved, same convention as the rest of this file."""

    def test_preserves_generator_return_value_as_metadata(self) -> None:
        # §E19: gptme's own _StreamWithMetadata.__iter__ reads a stream's
        # usage/cost metadata back off its generator's StopIteration.value.
        # The old wrapper's plain `for chunk in orig_gen: yield chunk` loop
        # discarded that value entirely.
        sentinel_metadata = {"usage": {"total_tokens": 42}}

        def fake_gen():
            yield "hello "
            yield "world"
            return sentinel_metadata

        class _FakeStream:
            def __init__(self) -> None:
                self.gen = fake_gen()

        wrapped = _wrap_thinking_stream(lambda *a, **kw: _FakeStream())
        stream_obj = wrapped()

        chunks: list[str] = []
        metadata = None
        gen = stream_obj.gen
        while True:
            try:
                chunks.append(next(gen))
            except StopIteration as stop:
                metadata = stop.value
                break

        self.assertEqual(chunks, ["hello ", "world"])
        self.assertEqual(metadata, sentinel_metadata)

    def test_missing_gen_attribute_degrades_without_raising(self) -> None:
        # §E19: a gptme version whose stream object has no `.gen` at all
        # (a shape change, or no _StreamWithMetadata) must degrade to no
        # live-thinking interception rather than raising AttributeError and
        # failing the whole episode.
        class _FakeStreamNoGen:
            pass

        wrapped = _wrap_thinking_stream(lambda *a, **kw: _FakeStreamNoGen())
        stream_obj = wrapped()  # must not raise
        self.assertFalse(hasattr(stream_obj, "gen"))

    def test_think_tags_stripped_from_yielded_chunks_and_emitted_once(self) -> None:
        # §E20l: the yielded chunk must have the <think>...</think> span
        # removed, not just extracted-and-also-left-in-place -- otherwise
        # the tags land in the stored message content too and get
        # re-extracted a second time downstream by dashboard/rendering.py.
        def fake_gen():
            yield "before <think>reasoning "
            yield "continues</think> after"
            return None

        class _FakeStream:
            def __init__(self) -> None:
                self.gen = fake_gen()

        wrapped = _wrap_thinking_stream(lambda *a, **kw: _FakeStream())
        stream_obj = wrapped()

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            chunks = list(stream_obj.gen)

        self.assertEqual(chunks, ["before ", " after"])
        for chunk in chunks:
            self.assertNotIn("<think>", chunk)
            self.assertNotIn("</think>", chunk)

        thinking_lines = [json.loads(line) for line in captured.getvalue().splitlines() if line.strip()]
        self.assertTrue(thinking_lines)
        self.assertTrue(all(e["type"] == "thinking" for e in thinking_lines))
        self.assertEqual("".join(e["content"] for e in thinking_lines), "reasoning continues")

    def test_chunk_with_no_think_tag_passes_through_unchanged(self) -> None:
        def fake_gen():
            yield "plain content, no tags"
            return None

        class _FakeStream:
            def __init__(self) -> None:
                self.gen = fake_gen()

        wrapped = _wrap_thinking_stream(lambda *a, **kw: _FakeStream())
        stream_obj = wrapped()
        self.assertEqual(list(stream_obj.gen), ["plain content, no tags"])


if __name__ == "__main__":
    unittest.main()
