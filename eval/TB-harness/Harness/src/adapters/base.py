from __future__ import annotations

from typing import Protocol, runtime_checkable

from environment.base import Environment
from harness_types import EpisodeBudget, EpisodeResult


@runtime_checkable
class AgentAdapter(Protocol):
    async def run_episode(self, prompt: str, env: Environment, budget: EpisodeBudget) -> EpisodeResult: ...
