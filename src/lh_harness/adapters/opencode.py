from __future__ import annotations

import json
import os
import posixpath
import re
import shlex

from ..agent_logs import visible_output as extract_opencode_visible_output
from ..agent_registry import normalise_reasoning_effort
from ..environment.base import Environment
from ..types import (
    DEFAULT_OPENCODE_MODEL,
    DEFAULT_TMP_DIR,
    DEFAULT_WORKSPACE_PATH,
    EpisodeBudget,
    EpisodeResult,
)
from ..utils.agent_cli import resolve_opencode_binary
from ..utils.platform_shell import env_prefix, shell_quote
from .cli_agent import CommandAgentAdapter

_CONFIG_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class OpenCodeAdapter(CommandAgentAdapter):
    """Run OpenCode headlessly through LongHorizon Harness.

    ``opencode run --format json`` streams one JSON event per line on stdout
    (``step_start``, ``tool_use``, ``text``, ``step_finish``, ``error``), which
    the shared agent_logs parsers normalize into the same views every other
    backend provides.  ``--yolo`` is OpenCode's documented alias of ``--auto``
    for headless runs; it approves every permission the harness already
    scopes for the episode.  Reasoning effort maps to ``--variant``, the
    provider-specific variant switch OpenCode exposes on ``opencode run``.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_OPENCODE_MODEL,
        api_key: str | None = None,
        base_url: str | None = None,
        workspace_path: str = DEFAULT_WORKSPACE_PATH,
        prompt_dir: str = f"{DEFAULT_TMP_DIR}/prompts",
        effort: str | None = None,
        reasoning_effort: str | None = None,
        role: str = "cli_executor",
        hidden_paths: tuple[str, ...] = (),
        mcp_config: str | None = None,
    ) -> None:
        normalized_model = model.strip()
        if not normalized_model:
            raise ValueError("OpenCode model must not be empty")
        if "\x00" in normalized_model or len(normalized_model) > 256:
            raise ValueError("OpenCode model contains invalid characters or is too long")
        # One shared validator for every backend: a length check alone would let
        # a quote or newline through into the command line.
        normalized_effort = normalise_reasoning_effort(
            reasoning_effort if reasoning_effort is not None else effort
        )

        opencode_binary = resolve_opencode_binary() or "opencode"
        env_assignments: list[tuple[str, str]] = []
        if api_key:
            # OpenCode falls back to OPENCODE_API_KEY when a provider has no
            # key of its own, so one harness credential covers every model.
            env_assignments.append(("OPENCODE_API_KEY", api_key))
        mcp_servers = _load_mcp_servers(mcp_config) if mcp_config else None
        if base_url or mcp_servers:
            provider_id = normalized_model.split("/", 1)[0].strip() or "opencode"
            config_path = _write_runtime_config(
                prompt_dir,
                provider_id,
                base_url,
                mcp_servers,
            )
            # OpenCode merges config files instead of replacing them, and
            # OPENCODE_CONFIG sits between the global and project configs, so
            # a per-run file carrying only the provider override and the
            # computer-use MCP servers keeps the user's own providers, models,
            # and MCP servers intact.
            env_assignments.append(("OPENCODE_CONFIG", config_path))

        command_parts = [
            shell_quote(opencode_binary),
            "run",
            "--format",
            "json",
            "--yolo",
            "--model",
            shell_quote(normalized_model),
        ]
        if normalized_effort:
            command_parts.extend(["--variant", shell_quote(normalized_effort)])
        # `opencode run` reads the prompt from stdin when no positional message
        # is given, keeping long prompts off the command line.
        command_parts.append("< {prompt_path}")

        env_prefix_text = env_prefix(env_assignments)
        super().__init__(
            command_template=f"{env_prefix_text}{' '.join(command_parts)}",
            prompt_dir=prompt_dir,
            workspace_path=workspace_path,
            visible_output_parser=extract_opencode_visible_output,
            hidden_paths=hidden_paths,
        )
        self.model = normalized_model
        self.effort = normalized_effort
        self.role = role

    async def run_episode(
        self,
        prompt: str,
        env: Environment,
        budget: EpisodeBudget,
        live_trajectory_path: str | None = None,
    ) -> EpisodeResult:
        result = await super().run_episode(
            prompt,
            env,
            budget,
            live_trajectory_path=live_trajectory_path,
        )
        result.metadata.update(
            {
                "opencode_role": self.role,
                "opencode_model": self.model,
                "opencode_variant": self.effort,
                "opencode_approval_mode": "yolo",
            }
        )
        return result


def _load_mcp_servers(mcp_config: str) -> dict[str, object]:
    """Load the `mcp` server map from a plugin-written OpenCode config file.

    The computer-use plugins store one OpenCode-format JSON per agent
    (``{"mcp": {name: {type, command, args, enabled}}}``); anything else is a
    configuration error rather than something to silently ignore.
    """

    try:
        with open(mcp_config, encoding="utf-8") as fh:
            payload = json.load(fh)
    except OSError as exc:
        raise ValueError(f"could not read OpenCode MCP config {mcp_config!r}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"OpenCode MCP config {mcp_config!r} is not valid JSON: {exc}") from exc
    servers = payload.get("mcp") if isinstance(payload, dict) else None
    if not isinstance(servers, dict) or not servers:
        raise ValueError(f"OpenCode MCP config {mcp_config!r} holds no `mcp` servers")
    return servers


def _write_runtime_config(
    prompt_dir: str,
    provider_id: str,
    base_url: str | None,
    mcp_servers: dict[str, object] | None,
) -> str:
    """Write an OPENCODE_CONFIG file with the provider override and/or the
    computer-use MCP servers.

    The harness runs OpenCode with ``cd <workspace> && ...``, so the config
    lives inside the run's own prompt directory instead of the workspace.
    """
    config: dict[str, object] = {}
    if base_url:
        normalized_url = base_url.strip().rstrip("/")
        if not normalized_url or "\x00" in normalized_url:
            raise ValueError("OpenCode base URL must be a non-empty endpoint")
        config["provider"] = {
            provider_id: {
                "options": {"baseURL": normalized_url},
            }
        }
    if mcp_servers:
        config["mcp"] = mcp_servers
    normalized_dir = prompt_dir.rstrip("/") or "."
    safe_provider = _CONFIG_FILENAME_RE.sub("_", provider_id).strip("_") or "opencode"
    path = posixpath.join(normalized_dir, f"opencode-runtime-{safe_provider}.json")
    try:
        os.makedirs(normalized_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise ValueError(f"could not write OpenCode runtime config: {exc}") from exc
    return path