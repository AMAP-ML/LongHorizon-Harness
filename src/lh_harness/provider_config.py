"""User provider configuration: any OpenAI-compatible endpoint, opencode default.

The harness talks to OpenAI-compatible ``/chat/completions`` endpoints only.
The **default** is OpenCode Zen (``https://opencode.ai/zen/v1`` with model
``opencode/deepseek-v4-flash-free``, the same dev target the adapter layer
was built against) so a fresh install works out of the box — but that default
is **user-customizable** through exactly one file:

    ~/.lh-harness/provider.json          (or $LH_HARNESS_PROVIDER_CONFIG)

with the shape:

    {
        "api_key": "sk-...",
        "base_url": "https://api.example.com/v1",
        "model": "gpt-5-mini"
    }

A sample with the default opencode values lives at the repository root as
``provider.example.json`` — copy it to ``~/.lh-harness/provider.json`` to
pin your own endpoint/model.

Precedence per field (highest first):

1. explicit constructor argument (``api_key=``/``base_url=``/``model=``)
2. ``LH_HARNESS_PROVIDER_API_KEY`` / ``LH_HARNESS_PROVIDER_BASE_URL`` /
   ``LH_HARNESS_PROVIDER_MODEL`` environment variables
3. the config file above
4. the generic ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` / ``OPENAI_MODEL``
   environment variables
5. the built-in default: OpenCode Zen (opencode's api key is read from
   ``OPENCODE_API_KEY``, the variable the OpenCode Zen CLI already uses)

Setting 1-4 for a field overrides the built-in default for that field only —
the default opencode endpoint is not all-or-nothing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE_NAME = "provider.json"
DEFAULT_CONFIG_PATH = Path.home() / ".lh-harness" / CONFIG_FILE_NAME

# The built-in default: OpenCode Zen, the endpoint this harness itself was
# developed against (mirrors the "testing on a weak free model is the correct
# development target" note in v1/provider.py). Any of the higher-precedence
# knobs above can change it per field.
DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"
DEFAULT_MODEL = "opencode/deepseek-v4-flash-free"

# The sample config a user gets on first run (and provider.example.json at
# the repo root, kept in sync by a comment there): the default endpoint,
# default model, blank api key (filled from the env or edited in place).
SAMPLE_SETTINGS = {
    "api_key": "",
    "base_url": DEFAULT_BASE_URL,
    "model": DEFAULT_MODEL,
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
    raw = os.getenv("LH_HARNESS_PROVIDER_CONFIG")
    return Path(raw).expanduser() if raw else DEFAULT_CONFIG_PATH


def read_config_file(path: Path | None = None) -> dict[str, str]:
    """Read the provider file; a missing file yields {}."""
    target = path or config_file_path()
    if not target.is_file():
        return {}
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
    return {str(key): str(value) for key, value in data.items() if str(key) in ("api_key", "base_url", "model")}


def ensure_user_config(path: Path | None = None) -> Path | None:
    """Create the user's provider config from the sample if it doesn't exist.

    Called from the CLI entry points, not from library code: materializing
    ``~/.lh-harness/provider.json`` (the sample's opencode default values)
    is what makes "customize the default" concrete — the user edits that
    file instead of the harness assuming anything. Returns the path
    written, or None if the file already existed.
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


def resolve(*, api_key: str = "", base_url: str = "", model: str = "") -> ProviderSettings:
    """Resolve the three provider fields with the documented precedence.

    ``base_url`` and ``model`` always come back populated: the built-in
    default is OpenCode Zen. ``api_key`` falls back to the generic
    ``OPENAI_API_KEY``, then ``OPENCODE_API_KEY`` (the variable the OpenCode
    Zen CLI itself reads), and may be empty — the caller decides whether a
    key-less call is acceptable.
    """
    file_data = read_config_file()
    resolved = ProviderSettings(
        api_key=api_key or _pick(
            ("LH_HARNESS_PROVIDER_API_KEY",),
            ("OPENAI_API_KEY", "OPENCODE_API_KEY"),
            file_data.get("api_key", ""),
        ),
        base_url=base_url or _pick(
            ("LH_HARNESS_PROVIDER_BASE_URL",),
            ("OPENAI_BASE_URL",),
            file_data.get("base_url", ""),
            DEFAULT_BASE_URL,
        ),
        model=model or _pick(
            ("LH_HARNESS_PROVIDER_MODEL",),
            ("OPENAI_MODEL",),
            file_data.get("model", ""),
            DEFAULT_MODEL,
        ),
    )
    if api_key:
        resolved.source = "argument"
    elif os.getenv("LH_HARNESS_PROVIDER_API_KEY"):
        resolved.source = "LH_HARNESS_PROVIDER_API_KEY"
    elif file_data.get("api_key"):
        resolved.source = str(config_file_path())
    elif os.getenv("OPENAI_API_KEY"):
        resolved.source = "OPENAI_API_KEY"
    elif os.getenv("OPENCODE_API_KEY"):
        resolved.source = "OPENCODE_API_KEY"
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
        f"  Add it to the config file ({config_file_path()}) or via "
        "LH_HARNESS_PROVIDER_API_KEY / OPENAI_API_KEY / OPENCODE_API_KEY "
        "environment variables (or a .env file)."
    )