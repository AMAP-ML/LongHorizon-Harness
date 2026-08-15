"""Agent CLI adapters with lazy public imports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import AgentAdapter
    from .claude_code import ClaudeCodeAdapter
    from .codex import CodexAdapter
    from .deepseek_harness import DeepSeekHarnessAdapter
    from .opencode import OpenCodeAdapter

_LAZY_EXPORTS = {
    "AgentAdapter": ".base",
    "ClaudeCodeAdapter": ".claude_code",
    "CodexAdapter": ".codex",
    "DeepSeekHarnessAdapter": ".deepseek_harness",
    "OpenCodeAdapter": ".opencode",
}

__all__ = [
    "AgentAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "DeepSeekHarnessAdapter",
    "OpenCodeAdapter",
]


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name, __name__), name)


def __dir__() -> list[str]:
    return sorted(__all__)
