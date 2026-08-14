"""GptmeAdapter — the only Writer backend: a tool-use loop, no agent CLI.

Drives gptme (github.com/gptme/gptme, MIT, ``pip install "kusudaemon[gptme]"``)
— a small tool-use loop (shell/read/save/patch) that talks to any OpenAI-compatible
endpoint — so the user's configured provider (see
``..provider_config``; default OpenCode Zen, customizable via
``./provider.json`` / ``KUSUDAEMON_PROVIDER_*`` /
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
    if model.startswith(("local/", "openai/")):
        return model
    if model.startswith("opencode/"):
        return f"local/{model[len('opencode/'):]}"
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
    # gptme's save/patch tools write out/<node>.md directly, so an empty
    # artifact after an episode means the agent genuinely produced nothing
    # (PLAN.md §D0) -- v0/runner.py must not paper over that with a chat
    # message.
    has_file_tools = True

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
        hidden_paths: tuple[str, ...] = (),
        hidden_path_exceptions: tuple[str, ...] = (),
    ) -> None:
        resolved = resolve(api_key=api_key or "", base_url=base_url or "", model=model or "")
        if not resolved.api_key:
            raise ValueError(
                "GptmeAdapter needs an API key: pass api_key=, set "
                "OPENAI_API_KEY / KUSUDAEMON_PROVIDER_API_KEY, or add it "
                "to a .env file (see provider_config.py)."
            )
        resolved_model = _gptme_model(resolved.model)

        env_parts = [
            f"OPENAI_BASE_URL={shlex.quote(resolved.base_url)}",
            f"OPENAI_API_KEY={shlex.quote(resolved.api_key)}",
            # PLAN-AUDIT.md §F3: gptme's own print_msg flushes, and this
            # worker's own prints pass flush=True, but a library `print` or
            # an uncaught traceback from a crashing episode can otherwise
            # sit in the block buffer until process exit -- bad for live
            # debugging, where the last few lines before a crash matter most.
            "PYTHONUNBUFFERED=1",
            # §E27: gptme's include_paths() auto-embeds the contents of every
            # path-like token in the prompt (chat.py, on every user message).
            # The harness prompt names run-dir paths -- the hidden-paths
            # notice lists events.jsonl/approvals.jsonl/audit/scratch/out and
            # the artifact instruction carries the absolute out/<node>.md path
            # -- so gptme read and dumped the run's own logs (full event log,
            # approvals, dir listings) into writer contexts, bypassing the tool
            # allowlist with plain open(). Observed on a live T1 run: writer
            # prompt 29461 -> 56095 chars of injected harness state; the model
            # looped on the repeated failure history in its own context.
            # GPTME_DISABLE_PATH_INCLUDE is gptme's documented lever for
            # programmatically-constructed prompts (autonomous mode).
            "GPTME_DISABLE_PATH_INCLUDE=1",
            # §E27: the active_context GENERATION_PRE hook is gated on
            # use_fresh_context() and injects the same files as a system
            # message; pin it off so isolation never depends on operator
            # config ([context].enabled / GPTME_FRESH).
            "GPTME_FRESH=0",
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
            visible_output_parser=extract_visible_output,
            hidden_paths=hidden_paths,
            hidden_path_exceptions=hidden_path_exceptions,
        )


from .trace_output import extract_visible_output, gptme_visible_output

