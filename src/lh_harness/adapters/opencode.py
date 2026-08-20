from __future__ import annotations

import json
import os
import posixpath
import re

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
        env_overrides: dict[str, str] = {}
        if api_key:
            # OpenCode falls back to OPENCODE_API_KEY when a provider has no
            # key of its own, so one harness credential covers every model.
            env_overrides["OPENCODE_API_KEY"] = api_key
        if base_url:
            provider_id = normalized_model.split("/", 1)[0].strip() or "opencode"
            config_path = _write_endpoint_config(prompt_dir, provider_id, base_url)
            # OpenCode merges config files instead of replacing them, and
            # OPENCODE_CONFIG sits between the global and project configs, so
            # a per-run file carrying only the provider override keeps the
            # user's own providers, models, and MCP servers intact.
            env_overrides["OPENCODE_CONFIG"] = config_path

        # `opencode run` reads the prompt from stdin when no positional message
        # is given, which is exactly how the adapter feeds every CLI.
        argv = [
            opencode_binary,
            "run",
            "--format",
            "json",
            "--yolo",
            "--model",
            normalized_model,
        ]
        if normalized_effort:
            argv.extend(["--variant", normalized_effort])

        super().__init__(
            argv=argv,
            env=env_overrides,
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


def _write_endpoint_config(prompt_dir: str, provider_id: str, base_url: str) -> str:
    """Write an OPENCODE_CONFIG file overriding one provider's base URL.

    The harness runs OpenCode with ``cd <workspace> && ...``, so the config
    lives inside the run's own prompt directory instead of the workspace.
    """
    normalized_url = base_url.strip().rstrip("/")
    if not normalized_url or "\x00" in normalized_url:
        raise ValueError("OpenCode base URL must be a non-empty endpoint")
    normalized_dir = prompt_dir.rstrip("/") or "."
    safe_provider = _CONFIG_FILENAME_RE.sub("_", provider_id).strip("_") or "opencode"
    config = {
        "provider": {
            provider_id: {
                "options": {"baseURL": normalized_url},
            }
        }
    }
    path = posixpath.join(normalized_dir, f"opencode-endpoint-{safe_provider}.json")
    try:
        os.makedirs(normalized_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise ValueError(f"could not write OpenCode endpoint config: {exc}") from exc
    return path