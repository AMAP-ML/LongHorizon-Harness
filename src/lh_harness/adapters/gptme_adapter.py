"""GptmeAdapter — the only Writer backend: a tool-use loop, no agent CLI.

Drives gptme (github.com/gptme/gptme, MIT, ``pip install "lh-harness[gptme]"``)
— a small tool-use loop (shell/read/save/patch) that talks to any OpenAI-compatible
endpoint — so the user's configured provider (see
``..provider_config``; default OpenCode Zen, customizable via
``./provider.json`` / ``LH_HARNESS_PROVIDER_*`` /
``OPENAI_*``) acts as the Writer itself. The same settings
``v1/provider.py`` already resolves for the Orchestrator/Reviewer feeds
this adapter's ``OPENAI_BASE_URL``/``OPENAI_API_KEY``.

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
import shlex
import sys
from pathlib import Path

from ..provider_config import resolve
from ..types import DEFAULT_TMP_DIR, DEFAULT_WORKSPACE_PATH
from .cli_agent import CommandAgentAdapter

# shell: bash. read/save: file read/write. patch: scoped file edits. Deliberately
# excludes gptme's browser/computer-use/MCP tools -- out of scope for a Writer
# leaf and each pulls in dependencies (playwright, etc.) this harness doesn't want.
DEFAULT_TOOL_ALLOWLIST: tuple[str, ...] = ("shell", "read", "save", "patch")

_WORKER_SCRIPT = Path(__file__).with_name("_gptme_worker.py")


def _gptme_model(model: str) -> str:
    """Map a provider-config model id onto gptme's ``local/`` prefix.

    gptme's model string needs the ``local/`` provider prefix (routes to
    whatever ``OPENAI_BASE_URL`` points at, per gptme/llm/models/data.py)
    rather than the bare id ``v1/provider.py`` sends to the same host's
    OpenAI-compatible endpoint. The model id itself is unchanged: only the
    routing prefix is added.
    """
    if "/" in model:
        if model.startswith(("local/", "openai/")):
            return model
        return f"local/{model.split('/', 1)[1]}"
    return f"local/{model}"


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
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        context_length: int | None = None,
        tool_allowlist: tuple[str, ...] = DEFAULT_TOOL_ALLOWLIST,
        tool_format: str = "markdown",
        workspace_path: str = DEFAULT_WORKSPACE_PATH,
        prompt_dir: str = f"{DEFAULT_TMP_DIR}/prompts",
        python_executable: str = sys.executable,
    ) -> None:
        resolved = resolve(api_key=api_key or "", base_url=base_url or "", model=model or "")
        if not resolved.api_key:
            raise ValueError(
                "GptmeAdapter needs an API key: pass api_key=, set "
                "OPENAI_API_KEY / LH_HARNESS_PROVIDER_API_KEY, or add it "
                "to a .env file (see provider_config.py)."
            )
        resolved_model = _gptme_model(resolved.model)

        env_parts = [
            f"OPENAI_BASE_URL={shlex.quote(resolved.base_url)}",
            f"OPENAI_API_KEY={shlex.quote(resolved.api_key)}",
        ]
        if context_length:
            env_parts.append(f"GPTME_CONTEXT_LENGTH={int(context_length)}")
        env_prefix = " ".join(env_parts) + " "

        command_parts = [
            shlex.quote(python_executable),
            shlex.quote(str(_WORKER_SCRIPT)),
            "--model",
            shlex.quote(resolved_model),
            "--tool-allowlist",
            shlex.quote(",".join(tool_allowlist)),
            "--tool-format",
            shlex.quote(tool_format),
        ]

        self.model = resolved_model
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
    module invents. It exists because the worker script controls the output
    shape — there is no external CLI whose format we'd have to tolerate.
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
