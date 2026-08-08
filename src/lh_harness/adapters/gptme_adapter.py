"""GptmeAdapter — a Writer backend with no agent CLI anywhere in the chain.

``ClaudeCodeAdapter``/``CodexAdapter`` shell out to an existing, pre-built
coding-agent CLI binary we don't control. This adapter instead drives
gptme (github.com/gptme/gptme, MIT, ``pip install gptme``) — a small
tool-use loop (shell/read/save/patch) that talks to any OpenAI-compatible
endpoint — so an arbitrary model, in particular this harness's own default
dev target (DeepSeek V4 Flash Free via OpenCode Zen, same host
``v1/provider.py`` already talks to for Orchestrator/Reviewer), can act as
the Writer itself.

It still subclasses ``CommandAgentAdapter`` and still shells a subprocess
— but of ``_gptme_worker.py``, a few lines of code in *this* repo that
call one gptme library function, not of a product someone else built and
we don't read. See that module's docstring for why an in-process call
(the more obvious reading of "no CLI at all") is actually the wrong
choice here: gptme's own ``chat()`` mutates process-global state
(``os.chdir``) and can't be forcibly cancelled once inside asyncio, both
of which a subprocess solves for free using machinery
(``environment/local.py``'s real timeout+killpg) every other adapter
already relies on.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from ..types import DEFAULT_TMP_DIR, DEFAULT_WORKSPACE_PATH
from .cli_agent import CommandAgentAdapter

# Matches v1/provider.py's own OpenCode Zen dev target (PLAN.md §12: "testing
# on a weak free model is the correct development target"). gptme's model
# string needs the "local/" provider prefix (routes to whatever
# OPENAI_BASE_URL points at, per gptme/llm/models/data.py) rather than the
# bare "opencode/deepseek-v4-flash-free" v1/provider.py uses directly against
# the same host's OpenAI-compatible endpoint.
DEFAULT_GPTME_MODEL = "local/deepseek-v4-flash-free"
DEFAULT_GPTME_BASE_URL = "https://opencode.ai/zen/v1"

# shell: bash. read/save: file read/write. patch: scoped file edits. Deliberately
# excludes gptme's browser/computer-use/MCP tools -- out of scope for a Writer
# leaf and each pulls in dependencies (playwright, etc.) this harness doesn't want.
DEFAULT_TOOL_ALLOWLIST: tuple[str, ...] = ("shell", "read", "save", "patch")

_WORKER_SCRIPT = Path(__file__).with_name("_gptme_worker.py")


class GptmeAdapter(CommandAgentAdapter):
    # gptme's own continuity model (re-point chat() at the same logdir) has
    # no fresh-vs-corrupted-log distinction the way Claude Code's
    # `--resume <session_id>` does -- see _gptme_worker.py. v0's runner
    # already falls back to a clean redispatch whenever this is False.
    supports_session_resume = False
    # tool_allowlist is baked in at construction, same as
    # ClaudeCodeAdapter.allowed_tools -- a node wanting a narrower set
    # constructs its own GptmeAdapter via the round loop's
    # writer_adapter_factory, same call shape as every other adapter.
    supports_tool_restriction = True

    def __init__(
        self,
        *,
        model: str = DEFAULT_GPTME_MODEL,
        base_url: str | None = None,
        api_key: str | None = None,
        context_length: int | None = None,
        tool_allowlist: tuple[str, ...] = DEFAULT_TOOL_ALLOWLIST,
        tool_format: str = "markdown",
        workspace_path: str = DEFAULT_WORKSPACE_PATH,
        prompt_dir: str = f"{DEFAULT_TMP_DIR}/prompts",
        python_executable: str = sys.executable,
    ) -> None:
        resolved_base_url = base_url or os.getenv(
            "LH_HARNESS_PROVIDER_BASE_URL", DEFAULT_GPTME_BASE_URL
        )
        resolved_api_key = (
            api_key
            or os.getenv("LH_HARNESS_PROVIDER_API_KEY")
            or os.getenv("OPENCODE_API_KEY")
        )
        if not resolved_api_key:
            raise ValueError(
                "GptmeAdapter needs an API key: pass api_key=, or set "
                "LH_HARNESS_PROVIDER_API_KEY / OPENCODE_API_KEY (the same "
                "lookup v1/provider.py already uses for OpenCode Zen)."
            )

        env_parts = [
            f"OPENAI_BASE_URL={shlex.quote(resolved_base_url)}",
            f"OPENAI_API_KEY={shlex.quote(resolved_api_key)}",
        ]
        if context_length:
            env_parts.append(f"GPTME_CONTEXT_LENGTH={int(context_length)}")
        env_prefix = " ".join(env_parts) + " "

        command_parts = [
            shlex.quote(python_executable),
            shlex.quote(str(_WORKER_SCRIPT)),
            "--model",
            shlex.quote(model),
            "--tool-allowlist",
            shlex.quote(",".join(tool_allowlist)),
            "--tool-format",
            shlex.quote(tool_format),
        ]

        self.model = model
        self.tool_allowlist = tuple(tool_allowlist)
        self._env_prefix = env_prefix
        self._command_parts = command_parts
        super().__init__(
            command_template=f"{env_prefix}{' '.join(command_parts)} < {{prompt_path}}",
            prompt_dir=prompt_dir,
            workspace_path=workspace_path,
            visible_output_parser=gptme_visible_output,
        )


def gptme_visible_output(raw: str) -> str:
    """Extract the final assistant message from the worker's
    ``--output-format json`` stdout: one JSON object per message,
    ``{"type": "message", "role": ..., "content": ...}`` — gptme's own
    documented structured-output mode (verified against a real
    ``pip install gptme``; not guessed from docs), not something this
    module invents. Deliberately separate from ``agent_logs.py``'s
    ``visible_output`` dispatcher rather than added as a fourth format
    there: that module's three formats all come from shelled-out agent
    CLIs this repo doesn't control the output shape of, while gptme's
    shape is ours to keep stable since we wrote the worker script that
    requests it.
    """
    last_assistant_text = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "message" or event.get("role") != "assistant":
            continue
        content = event.get("content")
        if isinstance(content, str) and content.strip():
            last_assistant_text = content
    return last_assistant_text
