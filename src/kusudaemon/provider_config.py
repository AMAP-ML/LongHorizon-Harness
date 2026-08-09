"""User provider configuration: named providers, opencode default, keys in .env.

The harness talks to OpenAI-compatible ``/chat/completions`` endpoints only.
The **default** is OpenCode Zen (``https://opencode.ai/zen/v1`` with model
``opencode/deepseek-v4-flash-free``) so a fresh install works out of the
box — but the default is **user-customizable** with exactly two files, both
at the repo root, both gitignored, both shipped as ``*.example`` templates
the user copies and edits:

- **Which providers exist and their endpoints**: ``provider.json`` (or
  ``$KUSUDAEMON_PROVIDER_CONFIG`` to point elsewhere) holds *named*
  providers. Each entry carries its ``base_url`` and ``model`` plus
  ``api_key_env`` — the name of the environment variable that holds that
  provider's key. Keys themselves never live in this file:

      {
        "default": "opencode",
        "providers": {
          "opencode": {
            "base_url": "https://opencode.ai/zen/v1",
            "model": "opencode/deepseek-v4-flash-free",
            "api_key_env": "OPENAI_API_KEY"
          }
        }
      }

  A flat legacy shape (no ``providers`` key) is accepted and treated as one
  provider named ``opencode``.

- **The keys themselves**: environment variables, loaded from a root
  ``.env`` file by CLI startup (see ``load_env_file``, or
  ``$KUSUDAEMON_ENV_FILE`` to point elsewhere — e.g. running the CLI from
  outside the project tree while keeping one ``.env`` in it), e.g.
  ``OPENAI_API_KEY=sk-...`` in ``.env`` for the opencode provider above.
  Add a provider to ``provider.json`` with ``"api_key_env": "DEEPSEEK_API_KEY"``
  and a matching ``DEEPSEEK_API_KEY=...`` line in ``.env`` to give it a key.

Which provider a call uses: explicit ``provider=`` argument >
``KUSUDAEMON_PROVIDER`` env var > the file's ``default`` > ``opencode``.

Per-field precedence (highest first):

1. explicit constructor argument (``api_key=``/``base_url=``/``model=``)
2. ``KUSUDAEMON_PROVIDER_API_KEY`` / ``KUSUDAEMON_PROVIDER_BASE_URL`` /
   ``KUSUDAEMON_PROVIDER_MODEL`` environment variables
3. the selected provider's entry in the config file (its ``base_url`` /
   ``model``; its key comes from the env var its ``api_key_env`` names)
4. the generic ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` / ``OPENAI_MODEL``
   environment variables
5. the built-in default: OpenCode Zen
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

# The built-in default: OpenCode Zen, the endpoint this harness itself was
# developed against (mirrors the "testing on a weak free model is the correct
# development target" note in v1/provider.py). Any of the higher-precedence
# knobs above can change it per field.
DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"
DEFAULT_MODEL = "opencode/deepseek-v4-flash-free"

# The sample config a user gets on first run (and provider.example.json at
# the repo root, kept in sync by a comment there): the default endpoint,
# default model, and the env var the key comes from (filled in .env).
SAMPLE_SETTINGS = {
    "default": DEFAULT_PROVIDER,
    "providers": {
        DEFAULT_PROVIDER: {
            "base_url": DEFAULT_BASE_URL,
            "model": DEFAULT_MODEL,
            "api_key_env": DEFAULT_API_KEY_ENV,
        }
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


def read_config_file(path: Path | None = None) -> dict[str, object]:
    """Read and normalize the provider file.

    Returns ``{"default": name, "providers": {name: {base_url, model,
    api_key_env}, ...}}``. A missing file yields an empty provider map, and
    ``opencode`` (the built-in default) applies at resolve time. The legacy
    flat shape ({"api_key", "base_url", "model"}) is normalized to a single
    provider named ``opencode``; a legacy ``api_key`` value is treated as
    the env var name the key lives in.
    """
    target = path or config_file_path()
    if not target.is_file():
        return {"default": DEFAULT_PROVIDER, "providers": {}}
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

    raw_providers = data.get("providers")
    if isinstance(raw_providers, dict):
        providers = {str(name): _normalize_entry(entry, target) for name, entry in raw_providers.items()}
        default = str(data.get("default") or DEFAULT_PROVIDER)
        if default not in providers:
            raise ProviderConfigError(
                f"provider config {target}: default {default!r} is not a "
                f"defined provider ({sorted(providers)})"
            )
        return {"default": default, "providers": providers}

    # Legacy flat shape: one provider named after the built-in default.
    return {"default": DEFAULT_PROVIDER, "providers": {DEFAULT_PROVIDER: _normalize_entry(data, target)}}


def _normalize_entry(entry: object, target: Path) -> dict[str, str]:
    if not isinstance(entry, dict):
        raise ProviderConfigError(f"provider config {target}: each provider must be an object")
    return {
        "base_url": str(entry.get("base_url") or ""),
        "model": str(entry.get("model") or ""),
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
    acceptable.
    """
    file_data = read_config_file()
    providers: dict[str, dict[str, str]] = file_data.get("providers") or {}  # type: ignore[assignment]
    name = (
        provider
        or os.getenv("KUSUDAEMON_PROVIDER")
        or str(file_data.get("default") or "")
        or DEFAULT_PROVIDER
    )
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
            DEFAULT_BASE_URL,
        ),
        model=model or _pick(
            ("KUSUDAEMON_PROVIDER_MODEL",),
            ("OPENAI_MODEL",),
            entry.get("model", ""),
            DEFAULT_MODEL,
        ),
    )
    if api_key:
        resolved.source = "argument"
    elif os.getenv("KUSUDAEMON_PROVIDER_API_KEY"):
        resolved.source = "KUSUDAEMON_PROVIDER_API_KEY"
    elif os.getenv(key_env):
        resolved.source = f"{key_env} (.env / environment)"
    return resolved


def require(settings: ProviderSettings) -> ProviderSettings:
    """Raise a clear error if the api key is missing, or return as-is.

    ``base_url``/``model`` can never be missing — the built-in opencode
    default covers them; only the api key has no bundled value.
    """
    if settings.api_key:
        return settings
    raise ProviderConfigError(
        "provider api key missing\n"
        f"  Add it to the .env file (e.g. OPENAI_API_KEY=...) or set "
        "OPENAI_API_KEY / KUSUDAEMON_PROVIDER_API_KEY in the environment."
    )