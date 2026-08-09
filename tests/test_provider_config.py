"""Provider config tests (src/lh_harness/provider_config.py).

The harness talks to the user's OpenAI-compatible endpoint with a built-in
opencode default. provider.json holds named providers (base_url/model +
which env var holds the key); api keys themselves live in the environment
/.env. Precedence per field: explicit argument > LH_HARNESS_PROVIDER_* env
> the selected provider entry > OPENAI_* env > built-in opencode default.

Coverage:
- the built-in default resolves when nothing else is set (opencode Zen)
- each precedence level wins over the ones below it
- provider selection: explicit arg > LH_HARNESS_PROVIDER env > file default;
  an unknown provider name raises
- a provider's api_key_env pulls its key from the env var it names
- legacy flat config shape still normalizes to a single provider
- malformed config raises a clear error
- ensure_user_config materializes the sample once, never overwrites
- require() passes when an api key is set and fails clearly when not
- env-file parsing/loading: dotenv subset, existing vars win, cwd search
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
    DEFAULT_PROVIDER,
    ProviderConfigError,
    ProviderSettings,
    config_file_path,
    ensure_user_config,
    load_env_file,
    parse_env_lines,
    read_config_file,
    require,
    resolve,
)

_ENV_KEYS = (
    "LH_HARNESS_PROVIDER_BASE_URL",
    "LH_HARNESS_PROVIDER_API_KEY",
    "LH_HARNESS_PROVIDER_MODEL",
    "LH_HARNESS_PROVIDER_CONFIG",
    "LH_HARNESS_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
)


class _EnvIsolatedTest(unittest.TestCase):
    def setUp(self) -> None:
        # Snapshot the *whole* environment, not just _ENV_KEYS: individual
        # tests set ad-hoc provider-specific vars (DEEPSEEK_API_KEY,
        # LEGACY_KEY, ...) that aren't in that fixed list, and a partial
        # snapshot silently leaks them into every test that runs after —
        # e.g. a test setting LH_HARNESS_PROVIDER=deepseek with no prior
        # value would never be unset, breaking unrelated tests elsewhere in
        # the suite that share this process.
        self._env_backup = dict(os.environ)
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["LH_HARNESS_PROVIDER_CONFIG"] = (
            str(Path(self._tmp.name) / "provider.json")
        )

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)
        self._tmp.cleanup()

    def _write_config(self, data: dict[str, object]) -> None:
        Path(self._tmp.name, "provider.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def _write_env(self, text: str) -> None:
        Path(self._tmp.name, ".env").write_text(text, encoding="utf-8")

    def _multi_provider_config(self) -> dict[str, object]:
        return {
            "default": "opencode",
            "providers": {
                "opencode": {
                    "base_url": "https://opencode.ai/zen/v1",
                    "model": "opencode/deepseek-v4-flash-free",
                    "api_key_env": "OPENAI_API_KEY",
                },
                "deepseek": {
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                    "api_key_env": "DEEPSEEK_API_KEY",
                },
            },
        }


class ResolveTest(_EnvIsolatedTest):
    def test_builtin_default_is_opencode(self) -> None:
        settings = resolve()
        self.assertEqual(settings.base_url, DEFAULT_BASE_URL)
        self.assertEqual(settings.model, DEFAULT_MODEL)
        self.assertEqual(settings.api_key, "")

    def test_scoped_env_overrides_provider_entry(self) -> None:
        self._write_config(self._multi_provider_config())
        os.environ["LH_HARNESS_PROVIDER_BASE_URL"] = "https://env.example.com/v1"
        settings = resolve()
        self.assertEqual(settings.base_url, "https://env.example.com/v1")
        self.assertEqual(settings.model, DEFAULT_MODEL)

    def test_provider_entry_overrides_generic_env(self) -> None:
        self._write_config(self._multi_provider_config())
        os.environ["OPENAI_BASE_URL"] = "https://generic.example.com/v1"
        os.environ["OPENAI_MODEL"] = "generic-model"
        settings = resolve()
        self.assertEqual(settings.base_url, "https://opencode.ai/zen/v1")
        self.assertEqual(settings.model, "opencode/deepseek-v4-flash-free")
        self.assertEqual(settings.api_key, "")

    def test_generic_env_overrides_default(self) -> None:
        os.environ["OPENAI_BASE_URL"] = "https://generic.example.com/v1"
        os.environ["OPENAI_MODEL"] = "generic-model"
        settings = resolve()
        self.assertEqual(settings.base_url, "https://generic.example.com/v1")
        self.assertEqual(settings.model, "generic-model")

    def test_apikey_env_fills_api_key_via_provider_api_key_env(self) -> None:
        self._write_config(self._multi_provider_config())
        os.environ["OPENAI_API_KEY"] = "openai-secret"
        settings = resolve()
        self.assertEqual(settings.api_key, "openai-secret")
        self.assertEqual(settings.source, "OPENAI_API_KEY (.env / environment)")

    def test_provider_specific_key_env_wins_over_generic(self) -> None:
        os.environ["OPENAI_API_KEY"] = "generic-secret"
        os.environ["DEEPSEEK_API_KEY"] = "deepseek-secret"
        self._write_config(self._multi_provider_config())
        settings = resolve(provider="deepseek")
        self.assertEqual(settings.api_key, "deepseek-secret")
        self.assertEqual(settings.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(settings.model, "deepseek-chat")

    def test_selection_explicit_arg_wins_choice(self) -> None:
        self._write_config(self._multi_provider_config())
        os.environ["LH_HARNESS_PROVIDER"] = "opencode"
        os.environ["OPENAI_API_KEY"] = "openai-secret"
        settings = resolve(provider="deepseek")
        self.assertEqual(settings.base_url, "https://api.deepseek.com/v1")

    def test_selection_env_var_wins_choice(self) -> None:
        self._write_config(self._multi_provider_config())
        os.environ["LH_HARNESS_PROVIDER"] = "deepseek"
        settings = resolve()
        self.assertEqual(settings.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(settings.model, "deepseek-chat")

    def test_selection_default_field_from_config(self) -> None:
        config = self._multi_provider_config()
        config["default"] = "deepseek"
        self._write_config(config)
        settings = resolve()
        self.assertEqual(settings.model, "deepseek-chat")

    def test_unknown_provider_raises(self) -> None:
        self._write_config(self._multi_provider_config())
        with self.assertRaises(ProviderConfigError) as ctx:
            resolve(provider="nonexistent")
        self.assertIn("deepseek", str(ctx.exception))
        self.assertIn("opencode", str(ctx.exception))

    def test_explicit_arguments_win_over_everything(self) -> None:
        self._write_config(self._multi_provider_config())
        os.environ["LH_HARNESS_PROVIDER_BASE_URL"] = "https://env.example.com/v1"
        settings = resolve(api_key="explicit", base_url="https://arg.example.com/v1", model="arg-model")
        self.assertEqual(settings.base_url, "https://arg.example.com/v1")
        self.assertEqual(settings.api_key, "explicit")
        self.assertEqual(settings.model, "arg-model")

    def test_scoped_env_api_key_overrides_provider_api_key_env(self) -> None:
        self._write_config(self._multi_provider_config())
        os.environ["OPENAI_API_KEY"] = "openai-secret"
        os.environ["LH_HARNESS_PROVIDER_API_KEY"] = "scoped-secret"
        settings = resolve()
        self.assertEqual(settings.api_key, "scoped-secret")
        self.assertEqual(settings.source, "LH_HARNESS_PROVIDER_API_KEY")

    def test_legacy_flat_shape_normalizes_to_single_provider(self) -> None:
        self._write_config({"base_url": "https://legacy.example.com/v1", "model": "legacy-model", "api_key": "LEGACY_KEY"})
        settings = resolve()
        self.assertEqual(settings.base_url, "https://legacy.example.com/v1")
        self.assertEqual(settings.model, "legacy-model")
        os.environ["LEGACY_KEY"] = "legacy-secret"
        settings = resolve()
        self.assertEqual(settings.api_key, "legacy-secret")

    def test_read_config_file_normalization(self) -> None:
        self._write_config(self._multi_provider_config())
        data = read_config_file()
        providers = data["providers"]
        self.assertIsInstance(providers, dict)
        self.assertEqual(providers["deepseek"]["api_key_env"], "DEEPSEEK_API_KEY")

    def test_malformed_config_json_raises(self) -> None:
        Path(self._tmp.name, "provider.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ProviderConfigError):
            resolve()

    def test_non_object_config_raises(self) -> None:
        Path(self._tmp.name, "provider.json").write_text("[1,2]", encoding="utf-8")
        with self.assertRaises(ProviderConfigError):
            resolve()


class EnsureUserConfigTest(_EnvIsolatedTest):
    def test_writes_sample_once_and_never_overwrites(self) -> None:
        path = config_file_path()
        self.assertFalse(path.exists())
        written = ensure_user_config()
        self.assertIsNotNone(written)
        self.assertTrue(path.exists())

        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["default"], DEFAULT_PROVIDER)
        self.assertEqual(data["providers"][DEFAULT_PROVIDER]["base_url"], DEFAULT_BASE_URL)
        self.assertEqual(data["providers"][DEFAULT_PROVIDER]["model"], DEFAULT_MODEL)
        self.assertEqual(data["providers"][DEFAULT_PROVIDER]["api_key_env"], "OPENAI_API_KEY")
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


class EnvFileLoaderTest(_EnvIsolatedTest):
    def test_parse_env_lines(self) -> None:
        text = (
            "# comment\n"
            "KEY=value\n"
            "export EXPORTED=1\n"
            "QUOTED='hello world'\n"
            "DOUBLE=\"a=b\"\n"
            "SPACES = trimmed  \n"
            "=bogus\n"
        )
        parsed = parse_env_lines(text)
        self.assertEqual(parsed["KEY"], "value")
        self.assertEqual(parsed["EXPORTED"], "1")
        self.assertEqual(parsed["QUOTED"], "hello world")
        self.assertEqual(parsed["DOUBLE"], "a=b")
        self.assertEqual(parsed["SPACES"], "trimmed")
        self.assertNotIn("", parsed)

    def test_load_env_file_sets_unset_vars(self) -> None:
        self._write_env("OPENAI_API_KEY=from-env-file\nLH_HARNESS_PROVIDER_MODEL=envfile-model\n")
        loaded = load_env_file(Path(self._tmp.name, ".env"))
        self.assertEqual(Path(self._tmp.name, ".env"), loaded)
        self.assertEqual(os.environ["OPENAI_API_KEY"], "from-env-file")
        self.assertEqual(os.environ["LH_HARNESS_PROVIDER_MODEL"], "envfile-model")

    def test_load_env_file_never_overrides_existing(self) -> None:
        os.environ["OPENAI_API_KEY"] = "real-shell"
        self._write_env("OPENAI_API_KEY=from-env-file\n")
        load_env_file(Path(self._tmp.name, ".env"))
        self.assertEqual(os.environ["OPENAI_API_KEY"], "real-shell")

    def test_load_env_file_missing_returns_none(self) -> None:
        self.assertIsNone(load_env_file(Path(self._tmp.name, "nope.env")))

    def test_load_env_file_finds_dotenv_in_cwd(self) -> None:
        old = Path.cwd()
        try:
            os.chdir(self._tmp.name)
            self._write_env("OPENAI_API_KEY=from-cwd\n")
            loaded = load_env_file()
            self.assertEqual(loaded, Path.cwd() / ".env")
            self.assertEqual(os.environ["OPENAI_API_KEY"], "from-cwd")
        finally:
            os.chdir(old)


if __name__ == "__main__":
    unittest.main()