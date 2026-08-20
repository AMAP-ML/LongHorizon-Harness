from __future__ import annotations

import importlib
import json
import os
import shlex

from ..types import (
    DEFAULT_CODEX_MODEL,
    DEFAULT_TMP_DIR,
    DEFAULT_WORKSPACE_PATH,
    EFFORT_CHOICES,
)
from ..agent_logs import visible_output as extract_codex_visible_output
from ..utils.agent_cli import resolve_codex_binary
from .cli_agent import CommandAgentAdapter

try:
    tomllib = importlib.import_module("tomllib")
except ModuleNotFoundError:
    tomllib = importlib.import_module("tomli")

# Codex resolves this provider id against `model_providers.<id>` so a run can
# target any OpenAI-compatible endpoint without editing ~/.codex/config.toml.
_PROVIDER_ID = "lh_harness"
_DEFAULT_BASE_URL = "https://api.openai.com/v1"

# Codex documents minimal|low|medium|high|xhigh for model_reasoning_effort:
# the scale's "min" spells out to "minimal" there, and "max" exists only on
# the Claude side, so it lands on Codex's top level.
_CODEX_EFFORTS = {
    "min": "minimal",
    "low": "low",
    "med": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
}


class CodexAdapter(CommandAgentAdapter):
    def __init__(
        self,
        *,
        model: str | None = DEFAULT_CODEX_MODEL,
        api_key: str | None = None,
        base_url: str | None = None,
        workspace_path: str = DEFAULT_WORKSPACE_PATH,
        prompt_dir: str = f"{DEFAULT_TMP_DIR}/prompts",
        mcp_config: str | None = None,
        add_dirs: list[str] | None = None,
        sandbox_mode: str | None = None,
        hidden_paths: tuple[str, ...] = (),
        effort: str | None = None,
    ) -> None:
        if effort is not None and effort not in EFFORT_CHOICES:
            raise ValueError(
                f"Codex effort must be one of {', '.join(EFFORT_CHOICES)}"
            )
        self.effort = effort
        self.effort_effective = _CODEX_EFFORTS[effort] if effort else None
        env_parts: list[str] = []
        if api_key:
            quoted_key = shlex.quote(api_key)
            env_parts.append(f"OPENAI_API_KEY={quoted_key}")
            env_parts.append(f"CODEX_API_KEY={quoted_key}")

        # Resolve once when an adapter is built.  A LongHorizon run must use
        # the same authenticated Codex installation as the desktop client when
        # both the standalone PATH CLI and ChatGPT.app are present.
        codex_binary = resolve_codex_binary() or "codex"
        command_parts = [
            shlex.quote(codex_binary),
            "exec",
            "--json",
            "--skip-git-repo-check",
        ]
        # Under its default sandbox `codex exec` cannot touch the filesystem,
        # which would block every CLI subtask. The harness already runs inside an
        # isolated environment, so bypass Codex's own sandbox unless the caller
        # picked an explicit policy.
        if sandbox_mode:
            command_parts.extend(["--sandbox", shlex.quote(sandbox_mode)])
        else:
            command_parts.append("--dangerously-bypass-approvals-and-sandbox")

        for override in _config_overrides(base_url=base_url, api_key=api_key):
            command_parts.extend(["-c", shlex.quote(override)])

        # MCP support is opt-in and uses Codex's own format: a TOML file holding
        # `[mcp_servers.*]` tables, replayed as `-c mcp_servers.<name>=...`
        # overrides because `--profile` only reads files inside $CODEX_HOME.
        mcp_config = mcp_config or os.getenv("LH_HARNESS_CODEX_MCP_CONFIG")
        if mcp_config:
            for override in mcp_server_overrides(mcp_config):
                command_parts.extend(["-c", shlex.quote(override)])

        resolved_add_dirs = list(add_dirs or [])
        env_add_dirs = os.getenv("LH_HARNESS_CODEX_ADD_DIRS") or os.getenv("LH_HARNESS_MCP_ADD_DIRS")
        if env_add_dirs:
            resolved_add_dirs.extend(part for part in env_add_dirs.split(os.pathsep) if part)
        for add_dir in resolved_add_dirs:
            command_parts.extend(["--add-dir", shlex.quote(add_dir)])

        if model:
            command_parts.extend(["--model", shlex.quote(model)])
        if self.effort_effective:
            command_parts.extend(
                [
                    "-c",
                    shlex.quote(
                        f"model_reasoning_effort={json.dumps(self.effort_effective)}"
                    ),
                ]
            )
        # `-` makes Codex read the prompt from stdin, keeping long prompts off
        # the command line and out of the process table.
        command_parts.append("-")

        env_prefix = (" ".join(env_parts) + " ") if env_parts else ""
        super().__init__(
            command_template=f"{env_prefix}{' '.join(command_parts)} < {{prompt_path}}",
            prompt_dir=prompt_dir,
            workspace_path=workspace_path,
            visible_output_parser=extract_codex_visible_output,
            hidden_paths=hidden_paths,
        )


def _config_overrides(*, base_url: str | None, api_key: str | None) -> list[str]:
    """Build the `-c key=value` overrides that point Codex at our endpoint."""
    if not base_url and not api_key:
        return []
    provider = {
        "name": "LongHorizon-Harness",
        "base_url": _normalize_base_url(base_url),
        "wire_api": "responses",
    }
    if api_key:
        provider["env_key"] = "OPENAI_API_KEY"
    return [
        f"model_providers.{_PROVIDER_ID}={_toml_inline(provider)}",
        f"model_provider={json.dumps(_PROVIDER_ID)}",
    ]


def _normalize_base_url(base_url: str | None) -> str:
    if not base_url:
        return _DEFAULT_BASE_URL
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


def _toml_inline(value) -> str:
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
