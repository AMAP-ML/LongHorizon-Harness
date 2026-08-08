import asyncio

from lh_harness.adapters.gptme_adapter import GptmeAdapter
from lh_harness.environment.local import LocalEnvironment
from lh_harness.types import EpisodeBudget


async def main():
    adapter = GptmeAdapter(workspace_path="/path/to/a/scratch/dir")
    result = await adapter.run_episode(
        "Write a haiku about recursion to haiku.txt and stop.",
        LocalEnvironment(), EpisodeBudget(max_duration_seconds=120),
    )
    print(result.status, result.metadata["assistant_visible_output"])


asyncio.run(main())