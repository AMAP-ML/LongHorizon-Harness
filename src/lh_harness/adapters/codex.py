from __future__ import annotations

import json
import os
import shlex

from ..types import DEFAULT_CODEX_MODEL, DEFAULT_TMP_DIR, DEFAULT_WORKSPACE_PATH
from ..agent_logs import visible_output as extract_codex_visible_output
from .cli_agent import CommandAgentAdapter

# Codex resolves this provider id against `model_providers.<id>` so a run can
# target any OpenAI-compatible endpoint without editing ~/.codex/config.toml.
_PROVIDER_ID = "lh_harness"
_DEFAULT_BASE_URL = "https://api.openai.com/v1"


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
    ) -> None:
        env_parts: list[str] = []
        if api_key:
            quoted_key = shlex.quote(api_key)
            env_parts.append(f"OPENAI_API_KEY={quoted_key}")
            env_parts.append(f"CODEX_API_KEY={quoted_key}")

        command_parts = [
            "codex",
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

        # MCP support is opt-in. Codex reads MCP servers from config, so a
        # Claude-style `.mcp.json` is translated into `-c mcp_servers.*`
        # overrides instead of being passed as a file.
        mcp_config = (
            mcp_config
            or os.getenv("LH_HARNESS_CODEX_MCP_CONFIG")
            or os.getenv("LH_HARNESS_MCP_CONFIG")
        )
        if mcp_config:
            for override in _mcp_config_overrides(mcp_config):
                command_parts.extend(["-c", shlex.quote(override)])

        resolved_add_dirs = list(add_dirs or [])
        env_add_dirs = os.getenv("LH_HARNESS_CODEX_ADD_DIRS") or os.getenv("LH_HARNESS_MCP_ADD_DIRS")
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
        super().__init__(
            command_template=f"{env_prefix}{' '.join(command_parts)} < {{prompt_path}}",
            prompt_dir=prompt_dir,
            workspace_path=workspace_path,
            visible_output_parser=extract_codex_visible_output,
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


def _mcp_config_overrides(path: str) -> list[str]:
    """Translate a Claude-style `.mcp.json` into Codex `mcp_servers` overrides."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []
    overrides: list[str] = []
    for name, spec in servers.items():
        if not isinstance(spec, dict) or not str(name).strip():
            continue
        entry = _mcp_server_entry(spec)
        if entry:
            overrides.append(f"mcp_servers.{name}={_toml_inline(entry)}")
    return overrides


def _mcp_server_entry(spec: dict) -> dict | None:
    url = spec.get("url")
    if isinstance(url, str) and url.strip():
        return {"url": url}
    command = spec.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    entry: dict = {"command": command}
    args = spec.get("args")
    if isinstance(args, list):
        string_args = [str(arg) for arg in args]
        if string_args:
            entry["args"] = string_args
    env = spec.get("env")
    if isinstance(env, dict) and env:
        entry["env"] = {str(key): str(value) for key, value in env.items()}
    return entry


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
