from __future__ import annotations

import json
import os
import posixpath
import re
import shlex

from ..agent_logs import visible_output as extract_opencode_visible_output
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

# OpenCode's --variant selects a named per-model preset. OpenAI-style models
# ship default variants named after the reasoning-effort levels themselves
# (minimal|low|medium|high|xhigh), so the scale's "min" and "med" spell out
# to "minimal" and "medium"; every other name passes through as-is - other providers ship a
# subset (e.g. high/max) and take custom variants from opencode.jsonc.
_VARIANT_FOR_EFFORT = {"min": "minimal", "med": "medium"}


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
        role: str = "cli_executor",
        hidden_paths: tuple[str, ...] = (),
    ) -> None:
        normalized_model = model.strip()
        if not normalized_model:
            raise ValueError("OpenCode model must not be empty")
        if "\x00" in normalized_model or len(normalized_model) > 256:
            raise ValueError("OpenCode model contains invalid characters or is too long")
        normalized_effort = (effort or "").strip()
        if len(normalized_effort) > 64:
            raise ValueError("OpenCode reasoning effort is too long")
        variant = _VARIANT_FOR_EFFORT.get(normalized_effort, normalized_effort)

        opencode_binary = resolve_opencode_binary() or "opencode"
        env_parts: list[str] = []
        if api_key:
            # OpenCode falls back to OPENCODE_API_KEY when a provider has no
            # key of its own, so one harness credential covers every model.
            env_parts.append(f"OPENCODE_API_KEY={shlex.quote(api_key)}")
        if base_url:
            provider_id = normalized_model.split("/", 1)[0].strip() or "opencode"
            config_path = _write_endpoint_config(prompt_dir, provider_id, base_url)
            # OpenCode merges config files instead of replacing them, and
            # OPENCODE_CONFIG sits between the global and project configs, so
            # a per-run file carrying only the provider override keeps the
            # user's own providers, models, and MCP servers intact.
            env_parts.append(f"OPENCODE_CONFIG={shlex.quote(config_path)}")

        command_parts = [
            shlex.quote(opencode_binary),
            "run",
            "--format",
            "json",
            "--yolo",
            "--model",
            shlex.quote(normalized_model),
        ]
        if variant:
            command_parts.extend(["--variant", shlex.quote(variant)])
        # `opencode run` reads the prompt from stdin when no positional message
        # is given, keeping long prompts off the command line.
        command_parts.append("< {prompt_path}")

        env_prefix = (" ".join(env_parts) + " ") if env_parts else ""
        super().__init__(
            command_template=f"{env_prefix}{' '.join(command_parts)}",
            prompt_dir=prompt_dir,
            workspace_path=workspace_path,
            visible_output_parser=extract_opencode_visible_output,
            hidden_paths=hidden_paths,
        )
        self.model = normalized_model
        self.effort = normalized_effort or None
        self.effort_effective = variant or None
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
                "opencode_variant": self.effort_effective or "",
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