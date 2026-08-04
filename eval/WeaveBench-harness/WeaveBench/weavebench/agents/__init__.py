"""Harness adapters for WeaveBench.

Each adapter wraps a third-party agent CLI / runtime so that the WeaveBench
orchestrator can drive it through a single interface. All adapters route
LLM calls through OpenRouter by default; override with
``--litellm_base_url`` / ``--litellm_api_key`` or the environment variables
``OPENROUTER_BASE_URL`` / ``OPENROUTER_API_KEY``.

Public adapters (the only 4 things users need to know about):
    * openclaw_agent.OpenClawAgent       — reference harness (3 transports)
    * codex_agent.CodexAgent             — OpenAI Codex CLI in-VM
    * claudecode_agent.ClaudeCodeAgent   — Anthropic Claude Code in-VM
    * hermes_agent.HermesAgent           — Nous Research Hermes in-VM
"""
from weavebench.agents.openclaw_agent import OpenClawAgent  # noqa: F401
from weavebench.agents.codex_agent import CodexAgent  # noqa: F401
from weavebench.agents.claudecode_agent import ClaudeCodeAgent  # noqa: F401
from weavebench.agents.hermes_agent import HermesAgent  # noqa: F401

__all__ = ["OpenClawAgent", "CodexAgent", "ClaudeCodeAgent", "HermesAgent"]
