"""Codex CLI Writer adapter (``codex exec --json``, OpenAI's CLI).

Ported 2026-08-13 from LongHorizon-Harness-main (MIT, see README Credits):
``adapters/codex.py``, adapted to this harness's invariants.

Auth is the CLI's own: ``OPENAI_API_KEY``/``CODEX_API_KEY`` from the
process environment or the explicit ``api_key`` constructor param, and
``codex``'s own ``~/.codex/config.toml`` when no overrides are given —
``pipeline/backends.py`` deliberately never threads the harness's
OpenAI-compatible provider credentials here (a zen/opencode key sent to
``api.openai.com``-bound tooling is a credential leak; see
``claude_code.py``'s docstring). ``base_url`` (with the optional
``wire_api``, default ``"responses"``) builds ``-c model_providers.*``
overrides pointing codex at an arbitrary OpenAI-compatible endpoint;
absent both, codex uses its own config untouched. The old adapter always
injected a default ``https://api.openai.com/v1`` override; this one
doesn't, so an operator's own codex provider/model configuration survives.

Like the Claude adapter, stdout runs through ``_agent_worker.py``'s
translator, so the tee'd trace is already gptme-shaped for every consumer
(dashboard, subagent-status deriver, session watcher) — and with
``has_file_tools = True`` the post-episode artifact fallback never runs
(PLAN.md §D0): codex edits the workspace itself, so an empty artifact is
an honest gate failure.

No session resume: ``supports_session_resume = False`` (v0/runner.py's
comment at the old Codex adapter names exactly this — codex has no
continuation mechanism for a prior thread from a new invocation, so the
runner redispatching a fresh episode is the only honest recovery).
"""

from __future__ import annotations

import importlib
import json
import os
import shlex
import sys
from pathlib import Path

from ..types import DEFAULT_TMP_DIR, DEFAULT_WORKSPACE_PATH
from .cli_agent import CommandAgentAdapter

try:
    tomllib = importlib.import_module("tomllib")
except ModuleNotFoundError:  # pragma: no cover — py<3.11
    tomllib = importlib.import_module("tomli")  # type: ignore[no-redef]

_WORKER_SCRIPT = Path(__file__).with_name("_agent_worker.py")
_PYTHON = sys.executable

# Codex resolves this provider id against `model_providers.<id>` so a run can
# target any OpenAI-compatible endpoint without editing ~/.codex/config.toml.
_PROVIDER_ID = "kusudaemon"


class CodexAdapter(CommandAgentAdapter):
    supports_session_resume = False
    supports_tool_restriction = False
    has_file_tools = True

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        wire_api: str = "responses",
        workspace_path: str = DEFAULT_WORKSPACE_PATH,
        prompt_dir: str = f"{DEFAULT_TMP_DIR}/prompts",
        mcp_config: str | None = None,
        add_dirs: list[str] | None = None,
        sandbox_mode: str | None = None,
        hidden_paths: tuple[str, ...] = (),
        hidden_path_exceptions: tuple[str, ...] = (),
    ) -> None:
        env_parts: list[str] = []
        key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("CODEX_API_KEY")
        if key:
            quoted_key = shlex.quote(key)
            env_parts.append(f"OPENAI_API_KEY={quoted_key}")
            env_parts.append(f"CODEX_API_KEY={quoted_key}")

        command_parts = [
            "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
        ]
        # Under its default sandbox `codex exec` cannot touch the filesystem,
        # which would block every Writer task. The harness already isolates
        # episodes, so bypass Codex's own sandbox unless the caller picked an
        # explicit policy.
        if sandbox_mode:
            command_parts.extend(["--sandbox", shlex.quote(sandbox_mode)])
        else:
            command_parts.append("--dangerously-bypass-approvals-and-sandbox")

        for override in _config_overrides(base_url=base_url, api_key=key, wire_api=wire_api):
            command_parts.extend(["-c", shlex.quote(override)])

        # MCP support is opt-in and uses Codex's own format: a TOML file holding
        # `[mcp_servers.*]` tables, replayed as `-c mcp_servers.<name>=...`
        # overrides because `--profile` only reads files inside $CODEX_HOME.
        mcp_config = mcp_config or os.getenv("KUSUDAEMON_CODEX_MCP_CONFIG")
        if mcp_config:
            for override in mcp_server_overrides(mcp_config):
                command_parts.extend(["-c", shlex.quote(override)])

        # add_dirs: extra directories Codex may read, rendered as --add-dir
        # flags. The env fallbacks (renamed from the LongHorizon-Harness
        # originals) keep operators' existing configuration working.
        resolved_add_dirs = list(add_dirs or [])
        env_add_dirs = os.getenv("KUSUDAEMON_CODEX_ADD_DIRS") or os.getenv(
            "KUSUDAEMON_MCP_ADD_DIRS"
        )
        if env_add_dirs:
            resolved_add_dirs.extend(part for part in env_add_dirs.split(os.pathsep) if part)
        for add_dir in resolved_add_dirs:
            command_parts.extend(["--add-dir", shlex.quote(add_dir)])

        if model:
            command_parts.extend(["--model", shlex.quote(model)])
        # `-` makes Codex read the prompt from stdin, keeping long prompts off
        # the command line and out of the process table.
        command_parts.append("-")

        env_prefix = (" ".join(env_parts) + " ") if env_parts else ""
        quoted_worker = shlex.quote(str(_WORKER_SCRIPT))
        command_template = (
            f"{env_prefix}{shlex.quote(_PYTHON)} {quoted_worker} --format codex -- "
            f"{' '.join(command_parts)} < {{prompt_path}}"
        )
        super().__init__(
            command_template=command_template,
            prompt_dir=prompt_dir,
            workspace_path=workspace_path,
            hidden_paths=hidden_paths,
            hidden_path_exceptions=hidden_path_exceptions,
        )


def _config_overrides(
    *, base_url: str | None, api_key: str | None, wire_api: str
) -> list[str]:
    """Build the `-c key=value` overrides that point Codex at an endpoint.

    ``None`` base_url and key mean "use codex's own config.toml" — no
    overrides at all (deviation from the source's always-inject-default,
    see the module docstring).
    """
    if not base_url and not api_key:
        return []
    provider: dict[str, object] = {
        "name": "Kusudaemon",
        "base_url": _normalize_base_url(base_url),
        "wire_api": wire_api,
    }
    if api_key:
        provider["env_key"] = "OPENAI_API_KEY"
    return [
        f"model_providers.{_PROVIDER_ID}={_toml_inline(provider)}",
        f"model_provider={json.dumps(_PROVIDER_ID)}",
    ]


def _normalize_base_url(base_url: str | None) -> str:
    if not base_url:
        return "https://api.openai.com/v1"
    trimmed = base_url.rstrip("/")
    # Codex requests `<base_url>/responses`, so the URL must carry the API
    # version segment that Anthropic-style base URLs usually omit.
    return trimmed if trimmed.endswith("/v1") else f"{trimmed}/v1"


def mcp_server_overrides(path: str) -> list[str]:
    """Read `[mcp_servers.*]` tables from a Codex TOML file as `-c` overrides."""
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    servers = data.get("mcp_servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []
    return [
        f"mcp_servers.{name}={_toml_inline(spec)}"
        for name, spec in servers.items()
        if isinstance(spec, dict) and spec and str(name).strip()
    ]


def _toml_inline(value: object) -> str:
    """Render a value as inline TOML, which is what `codex -c` parses."""
    if isinstance(value, dict):
        body = ", ".join(f"{key} = {_toml_inline(item)}" for key, item in value.items())
        return "{" + body + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_inline(item) for item in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))