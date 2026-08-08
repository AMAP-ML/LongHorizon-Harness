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
- API key resolution order (explicit arg > LH_HARNESS_PROVIDER_API_KEY >
  OPENCODE_API_KEY) and the loud failure when none are set
- env vars (OPENAI_BASE_URL/OPENAI_API_KEY/GPTME_CONTEXT_LENGTH) and the
  --tool-allowlist/--model/--tool-format flags reach the command line
- the worker script this adapter shells out to actually exists on disk
- gptme_visible_output correctly extracts the LAST assistant message from
  a multi-line --output-format json transcript, ignoring non-JSON lines,
  malformed JSON, and non-assistant roles
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from lh_harness.adapters.gptme_adapter import (  # noqa: E402
    _WORKER_SCRIPT,
    DEFAULT_GPTME_BASE_URL,
    DEFAULT_GPTME_MODEL,
    GptmeAdapter,
    gptme_visible_output,
)


class _EnvGuard:
    """Snapshots and restores the env vars GptmeAdapter reads, so tests
    never leak credentials into each other regardless of pass/fail."""

    _KEYS = ("LH_HARNESS_PROVIDER_BASE_URL", "LH_HARNESS_PROVIDER_API_KEY", "OPENCODE_API_KEY")

    def __enter__(self) -> "_EnvGuard":
        self._backup = {key: os.environ.pop(key, None) for key in self._KEYS}
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
            os.environ["OPENCODE_API_KEY"] = "env-key"
            adapter = GptmeAdapter(api_key="explicit-key")
            self.assertIn("explicit-key", adapter.command_template)
            self.assertNotIn("env-key", adapter.command_template)

    def test_provider_env_key_wins_over_opencode_fallback(self) -> None:
        with _EnvGuard():
            os.environ["LH_HARNESS_PROVIDER_API_KEY"] = "provider-key"
            os.environ["OPENCODE_API_KEY"] = "opencode-key"
            adapter = GptmeAdapter()
            self.assertIn("provider-key", adapter.command_template)
            self.assertNotIn("opencode-key", adapter.command_template)

    def test_opencode_fallback_used_when_provider_key_unset(self) -> None:
        with _EnvGuard():
            os.environ["OPENCODE_API_KEY"] = "opencode-key"
            adapter = GptmeAdapter()
            self.assertIn("opencode-key", adapter.command_template)


class CommandConstructionTest(unittest.TestCase):
    def test_default_model_and_base_url(self) -> None:
        adapter = GptmeAdapter(api_key="k")
        self.assertIn(DEFAULT_GPTME_MODEL, adapter.command_template)
        self.assertIn(DEFAULT_GPTME_BASE_URL, adapter.command_template)
        self.assertIn("OPENAI_BASE_URL=", adapter.command_template)
        self.assertIn("OPENAI_API_KEY=", adapter.command_template)

    def test_custom_model_and_base_url_override_defaults(self) -> None:
        adapter = GptmeAdapter(api_key="k", model="local/custom", base_url="https://example.com/v1")
        self.assertIn("local/custom", adapter.command_template)
        self.assertIn("https://example.com/v1", adapter.command_template)
        self.assertNotIn(DEFAULT_GPTME_BASE_URL, adapter.command_template)

    def test_tool_allowlist_reaches_command_line(self) -> None:
        adapter = GptmeAdapter(api_key="k", tool_allowlist=("shell", "read"))
        self.assertIn("--tool-allowlist", adapter.command_template)
        self.assertIn("shell,read", adapter.command_template)

    def test_context_length_omitted_by_default(self) -> None:
        adapter = GptmeAdapter(api_key="k")
        self.assertNotIn("GPTME_CONTEXT_LENGTH", adapter.command_template)

    def test_context_length_set_when_given(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
