"""Provider config tests (src/kusudaemon/provider_config.py).

provider.json is a flat map of backend name -> that backend's own config:
``gptme`` (the harness's own OpenAI-compatible endpoint, and the one the
direct-call reasoning provider shares) requires a non-empty ``providers``
map plus a ``default`` naming which one applies; ``claude``/``codex``/
``opencode`` are CLI-driven backends with their own auth and take a single
``model`` field -- a ``providers``/``provider`` key under any of them is a
loud ProviderConfigError, not an alternate shape. Api keys themselves live
in the environment/.env, referenced by name via ``api_key_env``.

Coverage:
- resolve() raises when nothing is configured, instead of silently
  substituting a hardcoded endpoint
- each precedence level wins over the ones below it
- provider selection: explicit arg > KUSUDAEMON_PROVIDER env > gptme.default
- a provider's api_key_env pulls its key from the env var it names
- legacy flat config shape still normalizes to a single gptme provider
- gptme without a 'providers' section raises
- claude/codex/opencode with a 'providers'/'provider' key raises
- unknown top-level keys raise
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
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.provider_config import (  # noqa: E402
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
    "KUSUDAEMON_PROVIDER_BASE_URL",
    "KUSUDAEMON_PROVIDER_API_KEY",
    "KUSUDAEMON_PROVIDER_MODEL",
    "KUSUDAEMON_PROVIDER_CONFIG",
    "KUSUDAEMON_PROVIDER",
    "KUSUDAEMON_ENV_FILE",
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
        # e.g. a test setting KUSUDAEMON_PROVIDER=deepseek with no prior
        # value would never be unset, breaking unrelated tests elsewhere in
        # the suite that share this process.
        self._env_backup = dict(os.environ)
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["KUSUDAEMON_PROVIDER_CONFIG"] = (
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
            "gptme": {
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
            },
        }


class ResolveTest(_EnvIsolatedTest):
    def test_raises_when_nothing_configured(self) -> None:
        # No provider.json, no KUSUDAEMON_PROVIDER_*/OPENAI_* env vars: no
        # base_url/model can be resolved from anywhere, so this must raise
        # rather than silently returning the opencode sample values.
        with self.assertRaises(ProviderConfigError) as ctx:
            resolve()
        self.assertIn("base_url", str(ctx.exception))
        self.assertIn("model", str(ctx.exception))

    def test_raises_names_only_the_missing_field(self) -> None:
        os.environ["OPENAI_BASE_URL"] = "https://generic.example.com/v1"
        with self.assertRaises(ProviderConfigError) as ctx:
            resolve()
        self.assertIn("model", str(ctx.exception))
        self.assertNotIn("base_url not configured", str(ctx.exception))

    def test_scoped_env_overrides_provider_entry(self) -> None:
        self._write_config(self._multi_provider_config())
        os.environ["KUSUDAEMON_PROVIDER_BASE_URL"] = "https://env.example.com/v1"
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

    def test_generic_env_used_when_no_file_or_scoped_env(self) -> None:
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
        os.environ["KUSUDAEMON_PROVIDER"] = "opencode"
        os.environ["OPENAI_API_KEY"] = "openai-secret"
        settings = resolve(provider="deepseek")
        self.assertEqual(settings.base_url, "https://api.deepseek.com/v1")

    def test_selection_env_var_wins_choice(self) -> None:
        self._write_config(self._multi_provider_config())
        os.environ["KUSUDAEMON_PROVIDER"] = "deepseek"
        settings = resolve()
        self.assertEqual(settings.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(settings.model, "deepseek-chat")

    def test_selection_default_field_from_config(self) -> None:
        config = self._multi_provider_config()
        config["gptme"]["default"] = "deepseek"
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
        os.environ["KUSUDAEMON_PROVIDER_BASE_URL"] = "https://env.example.com/v1"
        settings = resolve(api_key="explicit", base_url="https://arg.example.com/v1", model="arg-model")
        self.assertEqual(settings.base_url, "https://arg.example.com/v1")
        self.assertEqual(settings.api_key, "explicit")
        self.assertEqual(settings.model, "arg-model")

    def test_scoped_env_api_key_overrides_provider_api_key_env(self) -> None:
        self._write_config(self._multi_provider_config())
        os.environ["OPENAI_API_KEY"] = "openai-secret"
        os.environ["KUSUDAEMON_PROVIDER_API_KEY"] = "scoped-secret"
        settings = resolve()
        self.assertEqual(settings.api_key, "scoped-secret")
        self.assertEqual(settings.source, "KUSUDAEMON_PROVIDER_API_KEY")

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
        providers = data["gptme"]["providers"]
        self.assertIsInstance(providers, dict)
        self.assertEqual(providers["deepseek"]["api_key_env"], "DEEPSEEK_API_KEY")
        self.assertEqual(data["claude"], {})
        self.assertEqual(data["codex"], {})
        self.assertEqual(data["opencode"], {})

    def test_malformed_config_json_raises(self) -> None:
        Path(self._tmp.name, "provider.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ProviderConfigError):
            resolve()

    def test_non_object_config_raises(self) -> None:
        Path(self._tmp.name, "provider.json").write_text("[1,2]", encoding="utf-8")
        with self.assertRaises(ProviderConfigError):
            resolve()

    def test_gptme_without_providers_section_raises(self) -> None:
        self._write_config({"gptme": {"model": "some-model"}})
        with self.assertRaises(ProviderConfigError) as ctx:
            resolve()
        self.assertIn("providers", str(ctx.exception))

    def test_gptme_with_empty_providers_raises(self) -> None:
        self._write_config({"gptme": {"providers": {}}})
        with self.assertRaises(ProviderConfigError):
            resolve()

    def test_claude_with_providers_key_raises(self) -> None:
        self._write_config({"claude": {"model": "m", "providers": {"x": {"model": "m"}}}})
        with self.assertRaises(ProviderConfigError) as ctx:
            read_config_file()
        self.assertIn("claude", str(ctx.exception))

    def test_codex_with_provider_key_raises(self) -> None:
        self._write_config({"codex": {"provider": "openai"}})
        with self.assertRaises(ProviderConfigError):
            read_config_file()

    def test_opencode_with_providers_key_raises(self) -> None:
        self._write_config(
            {"opencode": {"provider": "zen", "providers": {"zen": {"model": "m"}}}}
        )
        with self.assertRaises(ProviderConfigError):
            read_config_file()

    def test_unknown_top_level_key_raises(self) -> None:
        self._write_config({"bogus_backend": {"model": "m"}})
        with self.assertRaises(ProviderConfigError) as ctx:
            read_config_file()
        self.assertIn("bogus_backend", str(ctx.exception))

    def test_missing_backend_key_defaults_to_empty(self) -> None:
        self._write_config(self._multi_provider_config())
        data = read_config_file()
        self.assertEqual(data["claude"], {})
        self.assertEqual(data["codex"], {})
        self.assertEqual(data["opencode"], {})

    def test_stale_gptme_default_falls_back_to_first_provider(self) -> None:
        config = self._multi_provider_config()
        config["gptme"]["default"] = "nonexistent"
        self._write_config(config)
        data = read_config_file()
        self.assertEqual(data["gptme"]["default"], "opencode")


class EnsureUserConfigTest(_EnvIsolatedTest):
    def test_writes_sample_once_and_never_overwrites(self) -> None:
        path = config_file_path()
        self.assertFalse(path.exists())
        written = ensure_user_config()
        self.assertIsNotNone(written)
        self.assertTrue(path.exists())

        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["gptme"]["default"], DEFAULT_PROVIDER)
        self.assertEqual(data["gptme"]["providers"][DEFAULT_PROVIDER]["base_url"], DEFAULT_BASE_URL)
        self.assertEqual(data["gptme"]["providers"][DEFAULT_PROVIDER]["model"], DEFAULT_MODEL)
        self.assertEqual(data["gptme"]["providers"][DEFAULT_PROVIDER]["api_key_env"], "OPENAI_API_KEY")
        self.assertIsNone(data["claude"]["model"])
        self.assertIsNone(data["codex"]["model"])
        self.assertEqual(data["opencode"]["model"], DEFAULT_MODEL)
        # The sample itself must satisfy the same validation rules a real
        # user file does (round-trips through read_config_file cleanly).
        path.write_text(json.dumps(data), encoding="utf-8")
        read_config_file()

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
        self._write_env("OPENAI_API_KEY=from-env-file\nKUSUDAEMON_PROVIDER_MODEL=envfile-model\n")
        loaded = load_env_file(Path(self._tmp.name, ".env"))
        self.assertEqual(Path(self._tmp.name, ".env"), loaded)
        self.assertEqual(os.environ["OPENAI_API_KEY"], "from-env-file")
        self.assertEqual(os.environ["KUSUDAEMON_PROVIDER_MODEL"], "envfile-model")

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

    def test_load_env_file_respects_kusudaemon_env_file_override(self) -> None:
        # cwd has no .env of its own, and is not an ancestor of self._tmp,
        # so the plain ancestor search would find nothing here.
        with tempfile.TemporaryDirectory() as elsewhere:
            self._write_env("OPENAI_API_KEY=from-override\n")
            os.environ["KUSUDAEMON_ENV_FILE"] = str(Path(self._tmp.name, ".env"))
            old = Path.cwd()
            try:
                os.chdir(elsewhere)
                loaded = load_env_file()
            finally:
                os.chdir(old)
            self.assertEqual(loaded, Path(self._tmp.name, ".env"))
            self.assertEqual(os.environ["OPENAI_API_KEY"], "from-override")

    def test_load_env_file_override_missing_returns_none(self) -> None:
        os.environ["KUSUDAEMON_ENV_FILE"] = str(Path(self._tmp.name, "nope.env"))
        self.assertIsNone(load_env_file())

    def test_load_env_file_falls_back_to_installed_repo_root(self) -> None:
        # Zero-config case: no KUSUDAEMON_ENV_FILE set, cwd (and its
        # ancestors) have no .env of their own, but the installed package's
        # own project root does -- e.g. `kusudaemon` invoked from a
        # downloads folder while credentials live at the dev checkout root.
        self._write_env("OPENAI_API_KEY=from-repo-root\n")
        with tempfile.TemporaryDirectory() as elsewhere:
            old = Path.cwd()
            try:
                os.chdir(elsewhere)
                with mock.patch(
                    "kusudaemon.provider_config._installed_repo_root",
                    return_value=Path(self._tmp.name),
                ):
                    loaded = load_env_file()
            finally:
                os.chdir(old)
            self.assertEqual(loaded, Path(self._tmp.name, ".env"))
            self.assertEqual(os.environ["OPENAI_API_KEY"], "from-repo-root")

    def test_load_env_file_no_fallback_available_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            old = Path.cwd()
            try:
                os.chdir(elsewhere)
                with mock.patch(
                    "kusudaemon.provider_config._installed_repo_root",
                    return_value=None,
                ):
                    self.assertIsNone(load_env_file())
            finally:
                os.chdir(old)


class ConfigFilePathTest(_EnvIsolatedTest):
    """config_file_path()'s cwd/ancestor/installed-repo-root search --
    mirrors EnvFileLoaderTest above one-for-one. Added after a real report
    of an edited provider.json being silently ignored: the CLI was invoked
    from a cwd that wasn't the repo root, config_file_path() only ever
    looked at cwd-relative "provider.json", found nothing, and resolve()
    fell all the way back to the built-in opencode default -- which
    happened to be byte-identical to the user's *previous* model value, so
    it looked like the edit had no effect rather than "file not found"."""

    def setUp(self) -> None:
        super().setUp()
        # Every other test in this file wants the config_file_path()
        # override on so writes are isolated; these tests are specifically
        # about what happens when that override is absent.
        os.environ.pop("KUSUDAEMON_PROVIDER_CONFIG", None)

    def test_config_file_path_finds_provider_json_in_cwd(self) -> None:
        old = Path.cwd()
        try:
            os.chdir(self._tmp.name)
            self._write_config(self._multi_provider_config())
            self.assertEqual(config_file_path(), Path.cwd() / "provider.json")
        finally:
            os.chdir(old)

    def test_config_file_path_finds_provider_json_in_ancestor(self) -> None:
        self._write_config(self._multi_provider_config())
        child = Path(self._tmp.name, "nested", "deeper")
        child.mkdir(parents=True)
        old = Path.cwd()
        try:
            os.chdir(child)
            # Path.cwd() (not self._tmp.name) on the right-hand side: macOS
            # resolves /tmp through a /var -> /private/var symlink, and
            # os.chdir + Path.cwd() round-trips through the same resolution
            # config_file_path()'s cwd.parents walk does, so comparing
            # against it (rather than the pre-chdir tempdir name) avoids a
            # spurious mismatch between equivalent paths.
            self.assertEqual(config_file_path(), Path.cwd().parent.parent / "provider.json")
        finally:
            os.chdir(old)

    def test_config_file_path_falls_back_to_installed_repo_root(self) -> None:
        self._write_config(self._multi_provider_config())
        with tempfile.TemporaryDirectory() as elsewhere:
            old = Path.cwd()
            try:
                os.chdir(elsewhere)
                with mock.patch(
                    "kusudaemon.provider_config._installed_repo_root",
                    return_value=Path(self._tmp.name),
                ):
                    self.assertEqual(config_file_path(), Path(self._tmp.name) / "provider.json")
            finally:
                os.chdir(old)

    def test_config_file_path_no_fallback_returns_cwd_relative_default(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            old = Path.cwd()
            try:
                os.chdir(elsewhere)
                with mock.patch(
                    "kusudaemon.provider_config._installed_repo_root",
                    return_value=None,
                ):
                    self.assertEqual(config_file_path(), Path("provider.json"))
            finally:
                os.chdir(old)

    def test_resolve_picks_up_edited_model_via_cwd_discovery(self) -> None:
        # The exact reported scenario: an edited provider.json model only
        # takes effect if config_file_path() actually finds that file.
        config = self._multi_provider_config()
        config["gptme"]["providers"]["opencode"]["model"] = "deepseek-v4-flash-free"
        self._write_config(config)
        old = Path.cwd()
        try:
            os.chdir(self._tmp.name)
            settings = resolve()
        finally:
            os.chdir(old)
        self.assertEqual(settings.model, "deepseek-v4-flash-free")

    def test_multiple_models_per_provider(self) -> None:
        from kusudaemon.provider_config import list_available_models
        config = {
            "gptme": {
                "default": "nvidia",
                "providers": {
                    "opencode": {
                        "base_url": "https://opencode.ai/zen/v1",
                        "model": "opencode/deepseek-v4-flash-free",
                        "models": ["opencode/deepseek-v4-flash-free", "opencode/llama-3.3-70b-instruct"],
                        "api_key_env": "OPENAI_API_KEY",
                    },
                    "nvidia": {
                        "base_url": "https://integrate.api.nvidia.com/v1",
                        "models": ["deepseek-ai/deepseek-v4-flash-0731", "meta/llama-3.3-70b-instruct"],
                        "api_key_env": "NVIDIA_API_KEY",
                    },
                },
            },
        }
        self._write_config(config)
        old = Path.cwd()
        try:
            os.chdir(self._tmp.name)
            models = list_available_models()
            self.assertIn("opencode/llama-3.3-70b-instruct", models)
            self.assertIn("meta/llama-3.3-70b-instruct", models)

            # Resolving with a model matching non-default provider matches provider
            settings = resolve(model="opencode/llama-3.3-70b-instruct")
            self.assertEqual(settings.base_url, "https://opencode.ai/zen/v1")
            self.assertEqual(settings.model, "opencode/llama-3.3-70b-instruct")
        finally:
            os.chdir(old)

    def test_list_available_models_includes_cli_backends(self) -> None:
        from kusudaemon.provider_config import list_available_models
        config = {
            "gptme": self._multi_provider_config()["gptme"],
            "claude": {"model": "claude-opus-5"},
            "codex": {"model": "gpt-5-codex"},
            "opencode": {"model": "opencode/qwen3-coder"},
        }
        self._write_config(config)
        old = Path.cwd()
        try:
            os.chdir(self._tmp.name)
            models = list_available_models()
            self.assertIn("claude-opus-5", models)
            self.assertIn("gpt-5-codex", models)
            self.assertIn("opencode/qwen3-coder", models)
        finally:
            os.chdir(old)

    def test_list_providers_with_models(self) -> None:
        from kusudaemon.provider_config import list_providers_with_models
        config = {
            "gptme": {
                "default": "nvidia",
                "providers": {
                    "opencode": {
                        "base_url": "https://opencode.ai/zen/v1",
                        "model": "opencode/deepseek-v4-flash-free",
                        "models": ["opencode/deepseek-v4-flash-free", "opencode/llama-3.3-70b-instruct"],
                        "api_key_env": "OPENAI_API_KEY",
                    },
                    "nvidia": {
                        "base_url": "https://integrate.api.nvidia.com/v1",
                        "models": ["deepseek-ai/deepseek-v4-flash-0731", "meta/llama-3.3-70b-instruct"],
                        "api_key_env": "NVIDIA_API_KEY",
                    },
                },
            },
        }
        self._write_config(config)
        old = Path.cwd()
        try:
            os.chdir(self._tmp.name)
            providers, default_provider = list_providers_with_models()
            self.assertEqual(
                providers["opencode"],
                ["opencode/deepseek-v4-flash-free", "opencode/llama-3.3-70b-instruct"],
            )
            # no primary `model` key -> `models` list as declared
            self.assertEqual(
                providers["nvidia"],
                ["deepseek-ai/deepseek-v4-flash-0731", "meta/llama-3.3-70b-instruct"],
            )
            self.assertEqual(default_provider, "nvidia")
        finally:
            os.chdir(old)

    def test_list_providers_with_models_primary_model_first(self) -> None:
        from kusudaemon.provider_config import list_providers_with_models
        config = {
            "gptme": {
                "default": "opencode",
                "providers": {
                    "opencode": {
                        "base_url": "https://opencode.ai/zen/v1",
                        "model": "opencode/deepseek-v4-flash-free",
                        "models": ["opencode/qwen3-coder"],
                        "api_key_env": "OPENAI_API_KEY",
                    },
                },
            },
        }
        self._write_config(config)
        old = Path.cwd()
        try:
            os.chdir(self._tmp.name)
            providers, default_provider = list_providers_with_models()
            self.assertEqual(providers["opencode"], ["opencode/deepseek-v4-flash-free", "opencode/qwen3-coder"])
            self.assertEqual(default_provider, "opencode")
        finally:
            os.chdir(old)

    def test_list_providers_with_models_default_falls_back_when_default_undefined(self) -> None:
        """The user's real-world shape: default names ``opencode`` but only
        ``nvidia``/``llama.cpp`` are defined — the UI must not hand back a
        provider that doesn't exist."""
        from kusudaemon.provider_config import list_providers_with_models
        config = {
            "gptme": {
                "default": "opencode",
                "providers": {
                    "nvidia": {
                        "base_url": "https://integrate.api.nvidia.com/v1",
                        "model": "deepseek-ai/deepseek-v4-flash-0731",
                        "api_key_env": "NVIDIA_API_KEY",
                    },
                },
            },
        }
        self._write_config(config)
        old = Path.cwd()
        try:
            os.chdir(self._tmp.name)
            providers, default_provider = list_providers_with_models()
            self.assertEqual(default_provider, "nvidia")
            self.assertEqual(list(providers), ["nvidia"])
        finally:
            os.chdir(old)

    def test_list_providers_with_models_empty_config(self) -> None:
        from kusudaemon.provider_config import DEFAULT_PROVIDER, list_providers_with_models
        # Explicit config_path: ConfigFilePathTest deliberately pops
        # KUSUDAEMON_PROVIDER_CONFIG, and the search would otherwise fall
        # back to the installed repo root's real provider.json.
        providers, default_provider = list_providers_with_models(
            config_path=Path(self._tmp.name) / "no-such-config.json"
        )
        self.assertEqual(providers, {})
        self.assertEqual(default_provider, DEFAULT_PROVIDER)

    def test_backend_settings_precedence_and_model_override(self) -> None:
        from kusudaemon.provider_config import read_backend_config, list_models_for_backend, ProviderConfigError
        config = {
            "claude": {
                "model": "claude-3-5-sonnet-latest",
                "api_key_env": "ANTHROPIC_API_KEY",
            },
            "codex": {
                "model": None,
                "api_key_env": "CODEX_API_KEY",
                "wire_api": "responses",
            },
            "opencode": {
                "model": "opencode/deepseek-v4-flash-free",
                "models": ["opencode/deepseek-v4-flash-free", "opencode/qwen3-coder"],
                "api_key_env": "OPENCODE_API_KEY",
            },
            "gptme": {
                "default": "opencode",
                "providers": {
                    "opencode": {
                        "base_url": "https://opencode.ai/zen/v1",
                        "model": "opencode/deepseek-v4-flash-free",
                        "api_key_env": "OPENCODE_API_KEY",
                    },
                },
            },
        }
        self._write_config(config)
        old = Path.cwd()
        try:
            os.chdir(self._tmp.name)
            # Test list_models_for_backend
            self.assertEqual(list_models_for_backend("opencode"), ["opencode/deepseek-v4-flash-free", "opencode/qwen3-coder"])
            self.assertEqual(list_models_for_backend("claude"), ["claude-3-5-sonnet-latest"])

            # Test claude resolution
            settings = read_backend_config("claude")
            self.assertEqual(settings.model, "claude-3-5-sonnet-latest")
            self.assertEqual(settings.api_key_env, "ANTHROPIC_API_KEY")

            # Test codex resolution with wire_api
            settings_codex = read_backend_config("codex")
            self.assertIsNone(settings_codex.model)
            self.assertEqual(settings_codex.extra.get("wire_api"), "responses")

            # Test opencode resolution -- base_url is never read for opencode
            settings_opencode = read_backend_config("opencode")
            self.assertEqual(settings_opencode.model, "opencode/deepseek-v4-flash-free")
            self.assertIsNone(settings_opencode.base_url)

            # Test model_override.json when candidate is in declared models
            run_dir = Path(self._tmp.name) / "run1"
            run_dir.mkdir()
            (run_dir / "model_override.json").write_text('{"model": "opencode/qwen3-coder"}', encoding="utf-8")
            settings_overridden = read_backend_config("opencode", run_dir=run_dir)
            self.assertEqual(settings_overridden.model, "opencode/qwen3-coder")
            self.assertEqual(settings_overridden.source, "model_override.json")

            # Test model_override.json when candidate is NOT in declared models (ignored)
            (run_dir / "model_override.json").write_text('{"model": "unknown-gpt-model"}', encoding="utf-8")
            settings_ignored = read_backend_config("opencode", run_dir=run_dir)
            self.assertEqual(settings_ignored.model, "opencode/deepseek-v4-flash-free")

            # Test explicit argument beats all
            settings_arg = read_backend_config("opencode", run_dir=run_dir, model="explicit-model")
            self.assertEqual(settings_arg.model, "explicit-model")

            # Test unknown backend raises
            with self.assertRaises(ProviderConfigError):
                read_backend_config("nonexistent_backend")
        finally:
            os.chdir(old)

    def test_claude_and_codex_optional_base_url_override(self) -> None:
        """Unlike opencode, claude/codex may still declare an explicit
        base_url override (e.g. routing through a proxy) -- only a
        'providers'/'provider' key is disallowed for them."""
        from kusudaemon.provider_config import read_backend_config
        config = {
            "claude": {"model": "claude-opus-5", "base_url": "https://proxy.example.com"},
        }
        self._write_config(config)
        old = Path.cwd()
        try:
            os.chdir(self._tmp.name)
            settings = read_backend_config("claude")
            self.assertEqual(settings.base_url, "https://proxy.example.com")
        finally:
            os.chdir(old)


if __name__ == "__main__":
    unittest.main()
