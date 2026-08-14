from __future__ import annotations

import posixpath
import shlex
import sys
from collections.abc import Callable, Sequence

from ..agent_logs import visible_output as extract_visible_output
from ..types import DEFAULT_DEEPSEEK_HARNESS_MODEL
from ..utils.agent_cli import resolve_dsh_binary
from .cli_agent import CommandAgentAdapter

_READ_ONLY_ROLES = {
    "manager",
    "final_response",
    "gui_auditor",
    "cli_auditor",
    "auditor_format_repair",
}
_WORKSPACE_WRITE_ROLES = {"gui_executor", "cli_executor"}


def permission_mode_for_role(role: str) -> str:
    if role in _READ_ONLY_ROLES:
        return "read-only"
    if role in _WORKSPACE_WRITE_ROLES:
        return "workspace-write"
    raise ValueError(f"unsupported DeepSeek Harness role: {role}")


class DeepSeekHarnessAdapter(CommandAgentAdapter):
    """Run DeepSeek Harness headlessly through LongHorizon Harness."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_DEEPSEEK_HARNESS_MODEL,
        api_key: str | None = None,
        base_url: str | None = None,
        workspace_path: str = ".",
        prompt_dir: str = ".lh-harness/tmp/prompts",
        dsh_home: str | None = None,
        role: str = "cli_executor",
        add_dirs: Sequence[str] = (),
        hidden_paths: Sequence[str] = (),
        visible_output_parser: Callable[[str], str] | None = None,
    ) -> None:
        normalized_model = model.strip()
        if not normalized_model:
            raise ValueError("DeepSeek Harness model must not be empty")
        if "\x00" in normalized_model or len(normalized_model) > 256:
            raise ValueError("DeepSeek Harness model contains invalid characters or is too long")
        if add_dirs:
            raise ValueError(
                "DeepSeek Harness phase-1 CLI integration does not support additional directories"
            )

        permission_mode = permission_mode_for_role(role)
        normalized_prompt_dir = prompt_dir.rstrip("/") or "."
        isolated_home = dsh_home or posixpath.join(
            posixpath.dirname(normalized_prompt_dir), "dsh-home"
        )
        dsh_binary = resolve_dsh_binary() or "dsh"

        environment = [
            f"DSH_HOME={shlex.quote(isolated_home)}",
            f"DSH_PERMISSION_MODE={shlex.quote(permission_mode)}",
        ]
        if api_key:
            environment.append(f"DEEPSEEK_API_KEY={shlex.quote(api_key)}")
        if base_url:
            environment.append(f"DEEPSEEK_BASE_URL={shlex.quote(base_url)}")

        command = [
            *environment,
            shlex.quote(sys.executable),
            "-m",
            "lh_harness.adapters.deepseek_runner",
            "--binary",
            shlex.quote(dsh_binary),
            "--prompt",
            "{prompt_path}",
            "--model",
            shlex.quote(normalized_model),
        ]
        super().__init__(
            command_template=" ".join(command),
            workspace_path=workspace_path,
            prompt_dir=prompt_dir,
            visible_output_parser=visible_output_parser or extract_visible_output,
            hidden_paths=hidden_paths,
        )
        self.model = normalized_model
        self.role = role
        self.permission_mode = permission_mode
        self.dsh_home = isolated_home
