"""Shared types for the recursive-decomposition harness (v0-v5, gptme backend).

Everything classic (manager/executor/auditor roles, HarnessConfig, AuditReport,
ManagedRound, prompt-language) was removed along with the Claude/Codex adapters
in the gptme-only cut. What remains is the shared surface the pipeline layers
actually touch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


def _launch_directory() -> str:
    """Directory lh-harness was started from, captured once at import."""
    try:
        return str(Path.cwd())
    except OSError:
        return os.environ.get("PWD") or "."


DEFAULT_WORKSPACE_PATH = _launch_directory()
# Repo-local, not $HOME: everything this harness writes lives inside the
# project folder it was launched from (matches provider_config.py's
# DEFAULT_CONFIG_PATH and pipeline/run.py's ./.lh-harness/runs default).
DEFAULT_TMP_DIR = f"{Path(DEFAULT_WORKSPACE_PATH) / '.lh-harness'}/tmp"


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    termination_reason: str | None = None


@dataclass
class EpisodeBudget:
    max_duration_seconds: int = 1800

    def __post_init__(self) -> None:
        if self.max_duration_seconds < 1:
            raise ValueError("max_duration_seconds must be at least 1")


@dataclass
class EpisodeResult:
    status: Literal["done", "timeout", "error", "cancelled"]
    actions_log: str = ""
    error: str | None = None
    duration_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)