"""Single OpenClaw entry point — picks the right transport under the hood.

WeaveBench bundles three implementations of the OpenClaw harness, one per
LLM API surface. End users should never have to choose by hand: pass
``transport=`` and we instantiate the right one.

    from weavebench.agents.openclaw_agent import OpenClawAgent

    agent = OpenClawAgent(transport="messages", model="openai/gpt-5.5")
    agent = OpenClawAgent(transport="responses", model="openai/gpt-5.5")
    agent = OpenClawAgent(transport="chat", model="google/gemini-3.1-pro-preview")

The three underlying classes (``_responses.OpenClawAgent``,
``_messages.OpenClawMessagesAgent``, ``_chat.OpenClawChatAgent``) live in
``weavebench/agents/_openclaw/`` — they're full implementations rather than
a single shared base because each pi-ai transport returns a slightly
different chat.jsonl shape and we'd rather keep the conversion logic
explicit than fold three slightly-different paths into one branchy class.
"""
from __future__ import annotations

from typing import Any


_TRANSPORTS = {"responses", "messages", "chat"}


def OpenClawAgent(*, transport: str = "messages", **kwargs: Any):  # noqa: N802 (factory mimics class name)
    """Return an OpenClaw agent for the requested LLM transport.

    Parameters
    ----------
    transport : {"responses", "messages", "chat"}, default "messages"
        Which LLM API surface to talk to.
          - ``"responses"``: OpenAI ``/v1/responses`` (gpt-5.x, gpt-5.x-codex)
          - ``"messages"``: Anthropic ``/v1/messages``  (claude-opus, claude-sonnet)
          - ``"chat"``: OpenAI Chat Completions ``/v1/chat/completions``
                       (anything OpenRouter exposes through the chat API)
    **kwargs
        Forwarded to the underlying agent class.
    """
    if transport not in _TRANSPORTS:
        raise ValueError(
            f"transport must be one of {sorted(_TRANSPORTS)}, got {transport!r}"
        )
    if transport == "responses":
        from weavebench.agents._openclaw._responses import OpenClawAgent as _Impl
    elif transport == "messages":
        from weavebench.agents._openclaw._messages import OpenClawMessagesAgent as _Impl
    else:  # transport == "chat"
        from weavebench.agents._openclaw._chat import OpenClawChatAgent as _Impl
    return _Impl(**kwargs)


# Re-export the shared policy constant so other agents (codex/claudecode/hermes)
# can keep their `from weavebench.agents.openclaw_agent import _TOOLING_POLICY`
# import without breaking.
from weavebench.agents._openclaw._responses import _TOOLING_POLICY  # noqa: E402, F401
