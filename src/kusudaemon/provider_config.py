"""User provider configuration: named providers, keys in .env.

The harness talks to OpenAI-compatible ``/chat/completions`` endpoints only.
There is no built-in fallback endpoint: ``resolve()`` raises
``ProviderConfigError`` if a ``base_url``/``model`` can't be found from an
explicit argument, a ``KUSUDAEMON_PROVIDER_*`` env var, the config file, or
a generic ``OPENAI_*`` env var (see "Per-field precedence" below) — a
misconfigured or unreadable provider file surfaces as a loud error, not a
silent switch to some other endpoint the caller didn't ask for. What a
*fresh* install gets is a **sample** ``provider.json`` (OpenCode Zen: see
``SAMPLE_SETTINGS``), materialized once by ``ensure_user_config()`` at CLI
startup so there's something to edit — that's a real file on disk the CLI
tells you it wrote, not a hidden runtime substitution. Configuration lives
in exactly two files, both at the repo root, both gitignored, both shipped
as ``*.example`` templates the user copies and edits:

- **Which backends exist and how they're configured**: ``provider.json``
  (or ``$KUSUDAEMON_PROVIDER_CONFIG`` to point elsewhere) is a **flat map
  of backend name to that backend's own config** — nothing else at the
  top level:

      {
        "gptme": {
          "default": "opencode",
          "providers": {
            "opencode": {
              "base_url": "https://opencode.ai/zen/v1",
              "model": "opencode/deepseek-v4-flash-free",
              "api_key_env": "OPENAI_API_KEY"
            }
          }
        },
        "claude": { "model": null },
        "codex": { "model": null, "wire_api": "responses" },
        "opencode": { "model": "opencode/deepseek-v4-flash-free" }
      }

  Only four keys are recognized (``SUPPORTED_BACKENDS`` = ``gptme``,
  ``claude``, ``codex``, ``opencode``); a key can simply be omitted when
  that backend isn't used. Each backend's shape reflects how it talks to
  a model, and the two shapes are **not interchangeable**:

  - **``gptme`` is the one backend that speaks the harness's own
    OpenAI-compatible protocol to an arbitrary endpoint**, so it
    *requires* a non-empty ``providers`` map (named entries, each with
    its own ``base_url``/``model``/``api_key_env``, optionally a
    ``models`` list of alternates) plus a ``default`` naming which one
    applies absent an explicit selection. A ``gptme`` block with no
    ``providers`` section is a loud ``ProviderConfigError``, not a
    silent fallback — there is nothing to pick a default *of*.  This same
    ``gptme`` block is also what the harness's own direct-call reasoning
    (classify/plan/review/…, ``v1/provider.py``'s
    ``OpenAICompatibleProvider``) resolves against: those calls and a
    ``gptme`` Writer episode share one provider selection, since both
    speak the identical protocol (see ``resolve()``).
  - **``claude``, ``codex``, and ``opencode`` are CLI-driven backends
    with their own auth** (the Claude Code / Codex / OpenCode CLIs each
    know how to reach their own vendor — Anthropic, OpenAI, OpenCode
    Zen — on their own). They take a single ``model`` field (optionally
    an ``api_key_env``/``base_url`` override for operators who route
    through a proxy, plus ``codex``'s ``wire_api``) and nothing more.
    **A ``providers`` (or ``provider``) key under any of these three is
    a ``ProviderConfigError``** — multi-endpoint selection is a ``gptme``
    concept, not a CLI-backend one. ``opencode`` in particular needs no
    ``base_url`` at all: the OpenCode CLI always talks to OpenCode Zen
    itself, so that field is never read for this backend.

  A flat legacy shape (bare ``base_url``/``model``/``api_key_env`` at the
  document root, no backend keys at all) is still accepted and treated as
  one ``gptme`` provider named ``opencode`` — the original single-provider
  shape, predating even the ``providers`` map.

- **The keys themselves**: environment variables, loaded from a root
  ``.env`` file by CLI startup (see ``load_env_file``, or
  ``$KUSUDAEMON_ENV_FILE`` to point elsewhere — e.g. running the CLI from
  outside the project tree while keeping one ``.env`` in it), e.g.
  ``OPENAI_API_KEY=sk-...`` in ``.env`` for the opencode provider above.
  Add a provider to ``gptme.providers`` with
  ``"api_key_env": "DEEPSEEK_API_KEY"`` and a matching
  ``DEEPSEEK_API_KEY=...`` line in ``.env`` to give it a key.

Which ``gptme`` provider a call uses: explicit ``provider=`` argument >
``KUSUDAEMON_PROVIDER`` env var > ``gptme.default`` in the file > the
built-in ``opencode`` name.

Per-field precedence (highest first), for the direct-call / ``gptme``
provider:

1. explicit constructor argument (``api_key=``/``base_url=``/``model=``)
2. ``KUSUDAEMON_PROVIDER_API_KEY`` / ``KUSUDAEMON_PROVIDER_BASE_URL`` /
   ``KUSUDAEMON_PROVIDER_MODEL`` environment variables
3. the selected provider's entry in ``gptme.providers`` (its ``base_url``
   / ``model``; its key comes from the env var its ``api_key_env`` names)
4. the generic ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` / ``OPENAI_MODEL``
   environment variables

If none of the above yields a ``base_url``/``model``, ``resolve()`` raises
``ProviderConfigError`` naming exactly which field is missing and which
config file it checked — there is no step 5 that silently falls back to a
hardcoded endpoint. ``read_backend_config()`` runs the equivalent ladder
for ``claude``/``codex``/``opencode`` (env var > ``model_override.json`` >
the backend's own block in the file), documented on that function.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE_NAME = "provider.json"
# Repo-root-relative (i.e. resolved against the current working directory),
# not a dotfile under $HOME: the harness's config lives entirely inside the
# project folder, next to provider.example.json, the same way .env sits
# next to .env.example. Run the CLI from the repo root (or set
# $KUSUDAEMON_PROVIDER_CONFIG) so this resolves to the right file.
DEFAULT_CONFIG_PATH = Path(CONFIG_FILE_NAME)

DEFAULT_PROVIDER = "opencode"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"

# OpenCode Zen, the endpoint this harness itself was developed against
# (mirrors the "testing on a weak free model is the correct development
# target" note in v1/provider.py). NOT a runtime fallback used by
# resolve() -- these two constants exist only to seed SAMPLE_SETTINGS, the
# file ensure_user_config() writes once for a fresh install to edit. A
# missing/unreadable config with no matching env var is a ProviderConfigError,
# never a silent substitution of these values.
DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"
DEFAULT_MODEL = "opencode/deepseek-v4-flash-free"

SUPPORTED_BACKENDS = ("gptme", "claude", "codex", "opencode")
# The three CLI-driven backends: each brings its own vendor auth, so a
# "providers" (or "provider") key under any of them is a configuration
# mistake, not an alternate shape -- read_config_file() rejects it loudly.
_CLI_BACKENDS = ("claude", "codex", "opencode")

# The sample config a user gets on first run (and provider.example.json at
# the repo root, kept in sync by a comment there): gptme's default endpoint
# and model behind a named provider, and the three CLI backends left
# unconfigured (they use their own login / CLI-side auth until a model is
# set here).
SAMPLE_SETTINGS = {
    "gptme": {
        "default": DEFAULT_PROVIDER,
        "providers": {
            DEFAULT_PROVIDER: {
                "base_url": DEFAULT_BASE_URL,
                "model": DEFAULT_MODEL,
                "api_key_env": DEFAULT_API_KEY_ENV,
            }
        },
    },
    "claude": {
        "model": None,
    },
    "codex": {
        "model": None,
        "wire_api": "responses",
    },
    "opencode": {
        "model": DEFAULT_MODEL,
    },
}


class ProviderConfigError(ValueError):
    pass


@dataclass
class ProviderSettings:
    api_key: str
    base_url: str
    model: str
    source: str = "unset"


@dataclass(frozen=True)
class BackendSettings:
    backend: str
    model: str | None          # None = omit the --model flag entirely
    base_url: str | None
    api_key: str               # resolved from api_key_env, may be ""
    api_key_env: str
    extra: dict[str, object]   # wire_api, permissions, ...
    source: str = "unset"


def config_file_path() -> Path:
    """Resolve the provider config file to actually read.

    ``KUSUDAEMON_PROVIDER_CONFIG`` is an explicit override and wins
    outright. Otherwise this mirrors ``load_env_file``'s search (that one
    was widened after a real report of a frozen/401 run caused by invoking
    ``kusudaemon`` from a cwd outside the project tree; ``provider.json``
    had the same narrow cwd-only lookup and never got the same fix): cwd,
    then each ancestor directory, then — as a last resort — the installed
    package's own project root (``_installed_repo_root()``), so an
    edited repo-root ``provider.json`` is found with zero shell
    configuration regardless of where the CLI is invoked from. Falls back
    to the plain cwd-relative path (matching the pre-fix behavior, and
    where ``ensure_user_config`` writes a fresh sample) when none of those
    exist yet.
    """
    raw = os.getenv("KUSUDAEMON_PROVIDER_CONFIG")
    if raw:
        return Path(raw).expanduser()
    cwd = Path.cwd()
    for candidate in (cwd, *cwd.parents):
        candidate_path = candidate / CONFIG_FILE_NAME
        if candidate_path.is_file():
            return candidate_path
    repo_root = _installed_repo_root()
    if repo_root is not None:
        candidate_path = repo_root / CONFIG_FILE_NAME
        if candidate_path.is_file():
            return candidate_path
    return DEFAULT_CONFIG_PATH


def _normalize_gptme_block(block: dict[str, object], target: Path) -> dict[str, object]:
    """Validate and normalize the ``gptme`` backend block.

    Returns ``{"default": name, "providers": {name: entry, ...},
    "fallbacks": {model: fallback_model, ...}}``. ``gptme`` is the one
    backend that speaks to an arbitrary OpenAI-compatible endpoint, so a
    non-empty ``providers`` map is mandatory -- there is no vendor default
    to fall back to the way ``claude``/``codex``/``opencode`` have one.
    """
    raw_providers = block.get("providers")
    if not isinstance(raw_providers, dict) or not raw_providers:
        raise ProviderConfigError(
            f"provider config {target}: backend 'gptme' requires a non-empty "
            "'providers' section (gptme talks to an arbitrary OpenAI-compatible "
            "endpoint, so each named provider needs its own base_url/model/"
            "api_key_env — see provider.example.json)"
        )
    providers = {str(name): _normalize_entry(entry, target) for name, entry in raw_providers.items()}
    default = str(block.get("default") or "") or next(iter(providers))
    if default not in providers:
        # A stale `default` name (e.g. left over after its provider entry
        # was deleted) used to make the whole file unreadable. The
        # declared providers are real and usable; fall back to the first
        # one so the config still resolves.
        default = next(iter(providers))

    raw_fallbacks = block.get("fallbacks")
    fallbacks: dict[str, str] = {}
    if isinstance(raw_fallbacks, dict):
        for k, v in raw_fallbacks.items():
            if isinstance(v, str) and v.strip():
                fallbacks[str(k).strip()] = v.strip()
            elif isinstance(v, (list, tuple)) and v:
                fallbacks[str(k).strip()] = str(v[0]).strip()

    return {"default": default, "providers": providers, "fallbacks": fallbacks}


def read_config_file(path: Path | None = None) -> dict[str, object]:
    """Read and normalize the provider file.

    Returns a flat ``{backend_name: block}`` map, one key per entry in
    ``SUPPORTED_BACKENDS`` (``gptme``/``claude``/``codex``/``opencode``),
    each defaulting to ``{}`` when the file doesn't declare it. ``gptme``'s
    block is always normalized to ``{"default", "providers", "fallbacks"}``
    (see ``_normalize_gptme_block``); the three CLI backends' blocks are
    passed through as-is except for the loud "providers"/"provider" key
    rejection described in the module docstring. A missing file yields all
    four keys empty. The oldest legacy shape ({"api_key", "base_url",
    "model"} at the document root, no backend keys at all) is normalized to
    a single ``gptme`` provider named ``opencode``; a legacy ``api_key``
    value is treated as the env var name the key lives in.
    """
    target = path or config_file_path()
    if not target.is_file():
        return {name: {} for name in SUPPORTED_BACKENDS}
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProviderConfigError(f"cannot read provider config {target}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderConfigError(f"invalid JSON in provider config {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProviderConfigError(f"provider config {target} must be a JSON object")

    if not any(name in data for name in SUPPORTED_BACKENDS) and ("base_url" in data or "model" in data):
        # Oldest legacy shape: the whole file IS one provider's fields.
        return {
            "gptme": {
                "default": DEFAULT_PROVIDER,
                "providers": {DEFAULT_PROVIDER: _normalize_entry(data, target)},
                "fallbacks": {},
            },
            "claude": {},
            "codex": {},
            "opencode": {},
        }

    unknown = sorted(set(data) - set(SUPPORTED_BACKENDS))
    if unknown:
        raise ProviderConfigError(
            f"provider config {target}: unknown top-level key(s) {unknown} "
            f"(provider.json is a flat map of backend name to config; "
            f"expected keys are {list(SUPPORTED_BACKENDS)})"
        )

    result: dict[str, object] = {}
    for name in SUPPORTED_BACKENDS:
        block = data.get(name)
        if block is None:
            result[name] = {}
            continue
        if not isinstance(block, dict):
            raise ProviderConfigError(f"provider config {target}: {name!r} must be an object")
        if name == "gptme":
            result[name] = _normalize_gptme_block(block, target)
        else:
            if "providers" in block or "provider" in block:
                raise ProviderConfigError(
                    f"provider config {target}: backend {name!r} does not support "
                    "multiple providers -- it uses its own CLI auth (Anthropic/"
                    "OpenAI/OpenCode Zen directly). Remove the 'providers'/"
                    "'provider' key and set 'model' directly."
                )
            result[name] = block
    return result


def list_providers_with_models(config_path: Path | None = None) -> tuple[dict[str, list[str]], str]:
    """Return ``({provider_name: [model, ...]}, default_provider_name)`` for
    the ``gptme`` backend's ``providers`` map.

    Ordered as declared in the config file; each provider's list carries its
    primary ``model`` first (``_normalize_entry`` guarantees this), so the
    first entry of the default provider is the sensible default selection.
    A missing/empty config yields an empty map with the built-in default
    name.
    """
    file_data = read_config_file(config_path)
    gptme = file_data.get("gptme")
    gptme = gptme if isinstance(gptme, dict) else {}
    raw_providers = gptme.get("providers")
    providers: dict[str, list[str]] = {}
    if isinstance(raw_providers, dict):
        for name, entry in raw_providers.items():
            if not isinstance(entry, dict):
                continue
            models = entry.get("models")
            if isinstance(models, list) and models:
                providers[str(name)] = [str(m) for m in models]
    default = str(gptme.get("default") or "") or DEFAULT_PROVIDER
    if default not in providers and providers:
        default = next(iter(providers))
    return providers, default


def list_models_for_backend(
    backend: str, config_path: Path | None = None, provider: str | None = None
) -> list[str]:
    """Collect all declared model names for a specific agent backend."""
    name = str(backend).strip().lower()
    if name not in SUPPORTED_BACKENDS:
        return []
    file_data = read_config_file(config_path)
    block = file_data.get(name)
    block = block if isinstance(block, dict) else {}
    models: list[str] = []

    def _add(m: object) -> None:
        if isinstance(m, str) and m.strip():
            val = m.strip()
            if val not in models:
                models.append(val)
        elif isinstance(m, (list, tuple)):
            for item in m:
                if isinstance(item, str) and item.strip():
                    val = item.strip()
                    if val not in models:
                        models.append(val)

    if name == "gptme":
        prov_name = provider or str(block.get("default") or DEFAULT_PROVIDER)
        providers = block.get("providers") if isinstance(block.get("providers"), dict) else {}
        p_info = providers.get(prov_name, {}) if isinstance(providers.get(prov_name), dict) else {}
        _add(p_info.get("model"))
        _add(p_info.get("models"))
    else:
        _add(block.get("model"))
        _add(block.get("models"))

    return models


def read_backend_config(
    backend: str,
    config_path: Path | None = None,
    *,
    run_dir: Path | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    extra: dict[str, object] | None = None,
    provider: str | None = None,
) -> BackendSettings:
    """Resolve configuration for an agent backend (claude, codex, opencode, gptme).

    Precedence ladder (highest first):
    1. Explicit arguments (model, base_url, api_key, extra; ``provider``
       selects which named provider the gptme backend talks to)
    2. KUSUDAEMON_<BACKEND>_MODEL / _BASE_URL / _API_KEY env vars
    3. Run-level model_override.json, ONLY when it names a model in this backend's models list
    4. The backend's own top-level block in provider.json
    5. Omit (None / CLI's own defaults)

    ``provider`` only affects the ``gptme`` backend (the one backend that
    uses the harness's OpenAI-compatible provider — claude/codex/opencode
    bring their own auth and endpoints, per Part II's adapters section).
    ``opencode``'s resolved ``base_url`` is always ``None``: the OpenCode
    CLI talks to OpenCode Zen itself regardless of what's configured here.
    """
    name = str(backend).strip().lower()
    if name not in SUPPORTED_BACKENDS:
        raise ProviderConfigError(f"unknown backend: {backend!r} (available: {list(SUPPORTED_BACKENDS)})")

    target = config_path or config_file_path()
    file_data = read_config_file(target)
    block = file_data.get(name)
    block = block if isinstance(block, dict) else {}

    cfg_model: str | None = None
    cfg_base_url: str | None = None
    cfg_api_key_env: str = ""
    cfg_extra: dict[str, object] = {}
    declared_models: list[str] = []

    if name == "gptme":
        prov_name = provider or str(block.get("default") or DEFAULT_PROVIDER)
        providers = block.get("providers") if isinstance(block.get("providers"), dict) else {}
        if prov_name not in providers:
            if providers:
                raise ProviderConfigError(
                    f"provider config {target}: gptme provider {prov_name!r} is not defined in providers ({sorted(providers)})"
                )
            prov_entry: dict[str, object] = {}
        else:
            prov_entry = providers[prov_name]
        raw_m = prov_entry.get("model")
        cfg_model = str(raw_m).strip() if raw_m else None
        raw_bu = prov_entry.get("base_url")
        cfg_base_url = str(raw_bu).strip() if raw_bu else None
        cfg_api_key_env = str(prov_entry.get("api_key_env") or DEFAULT_API_KEY_ENV)
        declared_models = list_models_for_backend("gptme", target, provider=prov_name)

    elif name == "opencode":
        raw_m = block.get("model")
        cfg_model = str(raw_m).strip() if raw_m else None
        # No base_url: the OpenCode CLI always talks to OpenCode Zen itself
        # (OpenCodeAdapter never reads a base_url), so it's not read here.
        cfg_api_key_env = str(block.get("api_key_env") or "OPENCODE_API_KEY")
        cfg_extra = {
            k: v for k, v in block.items()
            if k not in ("model", "api_key_env", "models")
        }
        declared_models = list_models_for_backend("opencode", target)

    elif name == "claude":
        raw_m = block.get("model")
        cfg_model = str(raw_m).strip() if raw_m else None
        raw_bu = block.get("base_url")
        cfg_base_url = str(raw_bu).strip() if raw_bu else None
        cfg_api_key_env = str(block.get("api_key_env") or "ANTHROPIC_API_KEY")
        cfg_extra = {
            k: v for k, v in block.items()
            if k not in ("model", "base_url", "api_key_env", "models")
        }
        declared_models = list_models_for_backend("claude", target)

    elif name == "codex":
        raw_m = block.get("model")
        cfg_model = str(raw_m).strip() if raw_m else None
        raw_bu = block.get("base_url")
        cfg_base_url = str(raw_bu).strip() if raw_bu else None
        cfg_api_key_env = str(block.get("api_key_env") or "CODEX_API_KEY")
        cfg_extra = {
            k: v for k, v in block.items()
            if k not in ("model", "base_url", "api_key_env", "models")
        }
        declared_models = list_models_for_backend("codex", target)

    # Precedence resolution
    # 1. API key
    resolved_api_key = ""
    api_key_source = ""
    env_api_key_var = f"KUSUDAEMON_{name.upper()}_API_KEY"
    if api_key:
        resolved_api_key = api_key
        api_key_source = "argument"
    elif os.getenv(env_api_key_var):
        resolved_api_key = os.environ[env_api_key_var]
        api_key_source = env_api_key_var
    elif cfg_api_key_env and os.getenv(cfg_api_key_env):
        resolved_api_key = os.environ[cfg_api_key_env]
        api_key_source = f"{cfg_api_key_env} (.env / environment)"

    # 2. Base URL
    resolved_base_url: str | None = None
    env_base_url_var = f"KUSUDAEMON_{name.upper()}_BASE_URL"
    if base_url:
        resolved_base_url = base_url
    elif os.getenv(env_base_url_var):
        resolved_base_url = os.environ[env_base_url_var]
    elif cfg_base_url:
        resolved_base_url = cfg_base_url

    # 3. Model
    resolved_model: str | None = None
    model_source = "default"
    env_model_var = f"KUSUDAEMON_{name.upper()}_MODEL"
    if model:
        resolved_model = model
        model_source = "argument"
    elif os.getenv(env_model_var):
        resolved_model = os.environ[env_model_var]
        model_source = env_model_var
    elif run_dir is not None and (run_dir / "model_override.json").is_file():
        try:
            ov_data = json.loads((run_dir / "model_override.json").read_text(encoding="utf-8"))
            if isinstance(ov_data, dict) and ov_data.get("model"):
                cand_model = str(ov_data["model"]).strip()
                if cand_model in declared_models:
                    resolved_model = cand_model
                    model_source = "model_override.json"
        except (OSError, json.JSONDecodeError):
            pass

    if resolved_model is None:
        if cfg_model:
            # Check validation against declared_models if present
            if declared_models and cfg_model not in declared_models:
                raise ProviderConfigError(
                    f"provider config {target}: backend {name!r} model {cfg_model!r} is not in declared models ({declared_models})"
                )
            resolved_model = cfg_model
            model_source = f"{name} (provider.json)"

    # 4. Extra
    final_extra = dict(cfg_extra)
    if extra:
        final_extra.update(extra)

    source = model_source if model_source != "default" else (api_key_source or "default")

    return BackendSettings(
        backend=name,
        model=resolved_model,
        base_url=resolved_base_url,
        api_key=resolved_api_key,
        api_key_env=cfg_api_key_env,
        extra=final_extra,
        source=source,
    )



def get_fallback_model(model: str, config_path: Path | None = None) -> str | None:
    """Look up fallback model for a given model from provider.json's
    ``gptme.fallbacks`` map (§G4's rate-limit fallback ladder — only the
    direct-call/gptme provider path has this mechanism)."""
    try:
        file_data = read_config_file(config_path)
        gptme = file_data.get("gptme")
        gptme = gptme if isinstance(gptme, dict) else {}
        fallbacks = gptme.get("fallbacks")
        if isinstance(fallbacks, dict):
            target = fallbacks.get(model)
            if target:
                return str(target)
    except Exception:
        pass
    return None


def get_model_for_role(
    role: str,
    default_model: str,
    run_dir: Path | None = None,
    role_models: dict[str, str] | None = None,
) -> str:
    """PLAN.md §G2: runtime override > per-role mapping > default model."""
    if run_dir is not None:
        override_file = run_dir / "model_override.json"
        if override_file.is_file():
            try:
                data = json.loads(override_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("model"):
                    return str(data["model"]).strip()
            except (OSError, json.JSONDecodeError):
                pass
    if role_models and role in role_models and role_models[role]:
        return str(role_models[role]).strip()
    return default_model



def _normalize_entry(entry: object, target: Path) -> dict[str, object]:
    if not isinstance(entry, dict):
        raise ProviderConfigError(f"provider config {target}: each provider must be an object")
    raw_models = entry.get("models")
    models_list: list[str] = []
    if isinstance(raw_models, (list, tuple)):
        models_list = [str(m).strip() for m in raw_models if str(m).strip()]
    elif isinstance(raw_models, str) and raw_models.strip():
        models_list = [raw_models.strip()]

    primary_model = str(entry.get("model") or "").strip()
    if primary_model and primary_model not in models_list:
        models_list.insert(0, primary_model)

    return {
        "base_url": str(entry.get("base_url") or ""),
        "model": primary_model or (models_list[0] if models_list else ""),
        "models": models_list,
        "api_key_env": str(entry.get("api_key_env") or entry.get("api_key") or DEFAULT_API_KEY_ENV),
    }


def ensure_user_config(path: Path | None = None) -> Path | None:
    """Create the user's provider config from the sample if it doesn't exist.

    Called from the CLI entry points, not from library code: materializing
    ``./provider.json`` (the sample's named opencode provider) is what
    makes "customize the default" concrete — the user edits that file
    instead of the harness assuming anything. Returns the path written, or
    None if the file already existed.
    """
    target = path or config_file_path()
    if target.is_file():
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(SAMPLE_SETTINGS, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return None
    return target


def parse_env_lines(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines (dotenv-style) into a dict.

    Minimal stdlib-only subset, matching what a root ``.env`` needs: blank
    lines and full-line ``#`` comments are skipped, an ``export `` prefix is
    accepted (bash-style), values may be single- or double-quoted (quotes
    stripped), and the split happens at the first ``=`` so values may
    contain ``=``. No interpolation, no line continuations.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        if "=" not in stripped:
            continue
        key, _, raw_value = stripped.partition("=")
        key = key.strip()
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def _installed_repo_root() -> Path | None:
    """Best-effort project root for the *installed* ``kusudaemon`` package.

    Walks up from this very file (not the cwd) looking for ``pyproject.toml``.
    For an editable install (``pip install -e``, this repo's normal dev
    setup) that resolves to the actual checkout regardless of where the CLI
    is invoked from — the mechanism ``load_env_file`` uses so that running
    ``kusudaemon`` from an unrelated directory (e.g. a downloads folder
    holding a source document) still finds the one ``.env`` at the project
    root with no shell configuration required. A non-editable/wheel install
    has no ``pyproject.toml`` alongside the installed code and this returns
    ``None`` — same as having no fallback at all, not an error.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def load_env_file(path: Path | None = None) -> Path | None:
    """Load a ``.env`` file's variables into ``os.environ``.

    With ``path=None``: if ``KUSUDAEMON_ENV_FILE`` is set, that exact path is
    used (an explicit escape hatch, e.g. for a non-editable install with no
    discoverable repo root — see ``_installed_repo_root``). Otherwise looks
    for ``.env`` in the current directory, then each ancestor up to the
    filesystem root, then — if still not found — at the installed package's
    own project root (``_installed_repo_root()``), so the root ``.env`` the
    README tells you to create is found automatically no matter where the
    CLI is invoked from, with zero shell configuration, as long as this is
    an editable/source install (the normal dev setup; see that helper's
    docstring for the one case it can't cover). Variables already set in
    the real environment are **never** overwritten (dotenv convention: the
    shell wins). Returns the path actually loaded, or ``None`` when no
    ``.env`` exists anywhere in that search.
    """
    if path is None:
        env_override = os.getenv("KUSUDAEMON_ENV_FILE")
        if env_override:
            path = Path(env_override).expanduser()
        else:
            cwd = Path.cwd()
            for candidate in (cwd, *cwd.parents):
                if (candidate / ".env").is_file():
                    path = candidate / ".env"
                    break
            else:
                repo_root = _installed_repo_root()
                if repo_root is not None and (repo_root / ".env").is_file():
                    path = repo_root / ".env"
    if path is None or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for key, value in parse_env_lines(text).items():
        os.environ.setdefault(key, value)
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    return path


def _pick(
    scoped_env: tuple[str, ...],
    generic_env: tuple[str, ...],
    file_value: str,
    default: str = "",
) -> str:
    for name in scoped_env:
        if os.getenv(name):
            return os.environ[name]
    if file_value:
        return file_value
    for name in generic_env:
        if os.getenv(name):
            return os.environ[name]
    return default


def resolve(*, provider: str = "", api_key: str = "", base_url: str = "", model: str = "") -> ProviderSettings:
    """Resolve the three provider fields with the documented precedence.

    ``base_url`` and ``model`` always come back populated: the built-in
    default is OpenCode Zen. The api key comes from the env var the
    selected provider's ``api_key_env`` names (default ``OPENAI_API_KEY``)
    and may be empty — the caller decides whether a key-less call is
    acceptable. Reads from ``provider.json``'s ``gptme`` block: this is the
    harness's own direct-call provider (classify/plan/review/...) and it
    shares its provider selection with the ``gptme`` Writer backend, since
    both speak the identical OpenAI-compatible protocol.
    """
    file_data = read_config_file()
    gptme = file_data.get("gptme")
    gptme = gptme if isinstance(gptme, dict) else {}
    providers: dict[str, dict[str, object]] = gptme.get("providers") or {}  # type: ignore[assignment]
    name = provider or os.getenv("KUSUDAEMON_PROVIDER")
    if not name and model:
        for p_name, p_entry in providers.items():
            if isinstance(p_entry, dict):
                p_m = str(p_entry.get("model") or "")
                p_ms = p_entry.get("models") or []
                if model == p_m or (isinstance(p_ms, (list, tuple)) and model in p_ms):
                    name = p_name
                    break
    if not name:
        name = str(gptme.get("default") or "") or DEFAULT_PROVIDER
    if not providers:
        # No config file at all: the built-in opencode default applies only
        # as the last-resort fallback, so generic OPENAI_* env vars still
        # beat it (empty entry -> _pick falls through to its default).
        entry: dict[str, str] = {}
    else:
        entry = providers.get(name)
        if entry is None:
            raise ProviderConfigError(
                f"provider {name!r} is not defined in {config_file_path()} "
                f"(available: {sorted(providers) or [DEFAULT_PROVIDER]})"
            )
    key_env = entry.get("api_key_env") or DEFAULT_API_KEY_ENV

    resolved = ProviderSettings(
        api_key=api_key or _pick(
            ("KUSUDAEMON_PROVIDER_API_KEY",),
            (key_env,),
            "",
        ),
        base_url=base_url or _pick(
            ("KUSUDAEMON_PROVIDER_BASE_URL",),
            ("OPENAI_BASE_URL",),
            entry.get("base_url", ""),
        ),
        model=model or _pick(
            ("KUSUDAEMON_PROVIDER_MODEL",),
            ("OPENAI_MODEL",),
            entry.get("model", ""),
        ),
    )
    if api_key:
        resolved.source = "argument"
    elif os.getenv("KUSUDAEMON_PROVIDER_API_KEY"):
        resolved.source = "KUSUDAEMON_PROVIDER_API_KEY"
    elif os.getenv(key_env):
        resolved.source = f"{key_env} (.env / environment)"
    if not resolved.base_url or not resolved.model:
        missing = ", ".join(
            field for field, value in (("base_url", resolved.base_url), ("model", resolved.model)) if not value
        )
        raise ProviderConfigError(
            f"provider {missing} not configured (checked {config_file_path()}, "
            "KUSUDAEMON_PROVIDER_BASE_URL/KUSUDAEMON_PROVIDER_MODEL, and "
            "OPENAI_BASE_URL/OPENAI_MODEL)\n"
            f"  Selected provider: {name!r}. Set it in provider.json's "
            f"'gptme.providers.{name}' entry (copy provider.example.json to "
            f"{CONFIG_FILE_NAME} at the repo root if it doesn't exist yet), "
            "or set the env vars above."
        )
    return resolved


def require(settings: ProviderSettings) -> ProviderSettings:
    """Raise a clear error if the api key is missing, or return as-is.

    ``base_url``/``model`` can never be missing on a ``ProviderSettings``
    that came from ``resolve()`` — it raises ``ProviderConfigError`` itself
    rather than silently substituting a built-in default when neither is
    configured. Only the api key has no such check baked into ``resolve()``
    (a key-less call may be a legitimate intermediate state for some
    callers), which is what this function covers.
    """
    if settings.api_key:
        return settings
    raise ProviderConfigError(
        "provider api key missing\n"
        f"  Add it to the .env file (e.g. OPENAI_API_KEY=...) or set "
        "OPENAI_API_KEY / KUSUDAEMON_PROVIDER_API_KEY in the environment."
    )


def list_available_models() -> list[str]:
    """Collect all model names declared anywhere in provider.json: every
    ``gptme`` provider's models, plus each CLI backend's own ``model``/
    ``models``."""
    models: list[str] = []

    def _add(m: object) -> None:
        if isinstance(m, str) and m.strip() and m.strip() not in models:
            models.append(m.strip())
        elif isinstance(m, (list, tuple)):
            for item in m:
                if isinstance(item, str) and item.strip() and item.strip() not in models:
                    models.append(item.strip())

    file_data = read_config_file()
    gptme = file_data.get("gptme")
    gptme = gptme if isinstance(gptme, dict) else {}
    providers: dict[str, dict[str, object]] = gptme.get("providers") or {}  # type: ignore[assignment]
    for p_info in providers.values():
        if isinstance(p_info, dict):
            _add(p_info.get("model"))
            _add(p_info.get("models"))
    for name in _CLI_BACKENDS:
        block = file_data.get(name)
        if isinstance(block, dict):
            _add(block.get("model"))
            _add(block.get("models"))
    try:
        res = resolve()
        if res.model and res.model not in models:
            models.insert(0, res.model)
    except Exception:
        if DEFAULT_MODEL not in models:
            models.append(DEFAULT_MODEL)
    return models
