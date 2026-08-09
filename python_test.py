import asyncio

from kusudaemon.adapters.gptme_adapter import GptmeAdapter
from kusudaemon.environment.local import LocalEnvironment
from kusudaemon.types import EpisodeBudget


async def main():
    adapter = GptmeAdapter(workspace_path="/path/to/a/scratch/dir")
    result = await adapter.run_episode(
        "Write a haiku about recursion to haiku.txt and stop.",
        LocalEnvironment(), EpisodeBudget(max_duration_seconds=120),
    )
    print(result.status, result.metadata["assistant_visible_output"])


asyncio.run(main())