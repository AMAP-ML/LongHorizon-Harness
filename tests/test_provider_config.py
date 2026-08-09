"""Provider config tests (src/lh_harness/provider_config.py).

The harness talks to the user's OpenAI-compatible endpoint with a built-in
opencode default that the user can override per field — precedence: explicit
argument > LH_HARNESS_PROVIDER_* env > ~/.lh-harness/provider.json >
OPENAI_* / OPENCODE_API_KEY env > built-in opencode default. These tests
freeze that resolution chain.

Coverage:
- the built-in default resolves when nothing else is set (opencode Zen)
- each precedence level wins over the ones below it (env, config file,
  generic env)
- unknown keys in the config file are ignored; a non-object raises
- malformed config JSON raises a clear error
- ensure_user_config materializes the sample once, never overwrites
- require() passes when an api key is set and fails clearly when not
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from lh_harness.provider_config import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ProviderConfigError,
    ProviderSettings,
    config_file_path,
    ensure_user_config,
    read_config_file,
    require,
    resolve,
)

_ENV_KEYS = (
    "LH_HARNESS_PROVIDER_BASE_URL",
    "LH_HARNESS_PROVIDER_API_KEY",
    "LH_HARNESS_PROVIDER_MODEL",
    "LH_HARNESS_PROVIDER_CONFIG",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "OPENCODE_API_KEY",
)


class ProviderConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._backup = {key: os.environ.pop(key, None) for key in _ENV_KEYS}
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["LH_HARNESS_PROVIDER_CONFIG"] = (
            str(Path(self._tmp.name) / "provider.json")
        )

    def tearDown(self) -> None:
        os.environ.pop("LH_HARNESS_PROVIDER_CONFIG", None)
        for key, value in self._backup.items():
            if value is not None:
                os.environ[key] = value
        self._tmp.cleanup()

    def _write_config(self, data: dict[str, str]) -> None:
        Path(self._tmp.name, "provider.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def test_builtin_default_is_opencode(self) -> None:
        settings = resolve()
        self.assertEqual(settings.base_url, DEFAULT_BASE_URL)
        self.assertEqual(settings.model, DEFAULT_MODEL)
        self.assertEqual(settings.api_key, "")

    def test_scoped_env_overrides_config_file(self) -> None:
        self._write_config({"base_url": "https://file.example.com/v1", "api_key": "file-key", "model": "file-model"})
        os.environ["LH_HARNESS_PROVIDER_BASE_URL"] = "https://env.example.com/v1"
        settings = resolve()
        self.assertEqual(settings.base_url, "https://env.example.com/v1")
        self.assertEqual(settings.model, "file-model")
        self.assertEqual(settings.api_key, "file-key")

    def test_config_file_overrides_generic_env(self) -> None:
        self._write_config({"base_url": "https://file.example.com/v1", "model": "file-model"})
        os.environ["OPENAI_BASE_URL"] = "https://generic.example.com/v1"
        os.environ["OPENAI_MODEL"] = "generic-model"
        settings = resolve()
        self.assertEqual(settings.base_url, "https://file.example.com/v1")
        self.assertEqual(settings.model, "file-model")

    def test_generic_env_overrides_default(self) -> None:
        os.environ["OPENAI_BASE_URL"] = "https://generic.example.com/v1"
        os.environ["OPENAI_MODEL"] = "generic-model"
        settings = resolve()
        self.assertEqual(settings.base_url, "https://generic.example.com/v1")
        self.assertEqual(settings.model, "generic-model")

    def test_opencode_key_env_fills_api_key(self) -> None:
        os.environ["OPENCODE_API_KEY"] = "opencode-secret"
        settings = resolve()
        self.assertEqual(settings.api_key, "opencode-secret")

    def test_aliased_generic_key_env_fills_api_key(self) -> None:
        os.environ["OPENAI_API_KEY"] = "generic-secret"
        settings = resolve()
        self.assertEqual(settings.api_key, "generic-secret")

    def test_explicit_arguments_win_over_everything(self) -> None:
        self._write_config({"base_url": "https://file.example.com/v1", "model": "file-model"})
        os.environ["LH_HARNESS_PROVIDER_BASE_URL"] = "https://env.example.com/v1"
        settings = resolve(api_key="explicit", base_url="https://arg.example.com/v1", model="arg-model")
        self.assertEqual(settings.base_url, "https://arg.example.com/v1")
        self.assertEqual(settings.api_key, "explicit")
        self.assertEqual(settings.model, "arg-model")

    def test_read_config_file_ignores_unknown_keys(self) -> None:
        self._write_config({"api_key": "k", "base_url": "https://x/v1", "bogus": "ignored"})
        data = read_config_file()
        self.assertEqual(data, {"api_key": "k", "base_url": "https://x/v1"})

    def test_malformed_config_json_raises(self) -> None:
        Path(self._tmp.name, "provider.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ProviderConfigError):
            resolve()

    def test_non_object_config_raises(self) -> None:
        path = Path(self._tmp.name, "provider.json")
        path.write_text("[1,2]", encoding="utf-8")
        with self.assertRaises(ProviderConfigError):
            resolve()


class EnsureUserConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._backup = {key: os.environ.pop(key, None) for key in _ENV_KEYS}
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["LH_HARNESS_PROVIDER_CONFIG"] = (
            str(Path(self._tmp.name) / "provider.json")
        )

    def tearDown(self) -> None:
        os.environ.pop("LH_HARNESS_PROVIDER_CONFIG", None)
        for key, value in self._backup.items():
            if value is not None:
                os.environ[key] = value
        self._tmp.cleanup()

    def test_writes_sample_once_and_never_overwrites(self) -> None:
        path = config_file_path()
        self.assertFalse(path.exists())
        written = ensure_user_config()
        self.assertIsNotNone(written)
        self.assertTrue(path.exists())

        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["base_url"], DEFAULT_BASE_URL)
        self.assertEqual(data["model"], DEFAULT_MODEL)
        path.write_text("{customized}", encoding="utf-8")
        self.assertIsNone(ensure_user_config())
        self.assertEqual(path.read_text(encoding="utf-8"), "{customized}")

    def test_existing_file_left_alone(self) -> None:
        path = config_file_path()
        path.write_text("{}", encoding="utf-8")
        self.assertIsNone(ensure_user_config())
        self.assertEqual(path.read_text(encoding="utf-8"), "{}")


class RequireTest(unittest.TestCase):
    def test_require_passes_with_key(self) -> None:
        settings = ProviderSettings(api_key="k", base_url="https://x/v1", model="m")
        self.assertIs(require(settings), settings)

    def test_require_fails_without_key(self) -> None:
        settings = ProviderSettings(api_key="", base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL)
        with self.assertRaises(ProviderConfigError):
            require(settings)


if __name__ == "__main__":
    unittest.main()