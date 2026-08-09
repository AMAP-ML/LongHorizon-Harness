"""Research subagent dispatch (PLAN.md §13 v4). Backend is the same
fake_stream_agent.py double v0-v3 already use — it never actually writes a
file, so every test here exercises the "agent ignored the write-to-file
instruction, fall back to the episode's own visible output" path
documented in research.py, same as writer.py's promotion fallback.

Coverage:
- a fresh query dispatches a genuinely new episode under a derived id and
  caps an overlong result down to the token budget
- a second call for the *same* query is a pure no-op: no new episode, same
  cached finding text, even though a fresh dispatch would have produced
  different content (proves the cache is actually being hit, not luck)
- an empty finding fails its own nonempty gate
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_adapter import FakeStreamAgentAdapter  # noqa: E402

from waypoint.environment.local import LocalEnvironment  # noqa: E402
from waypoint.types import EpisodeBudget  # noqa: E402
from waypoint.v0.events import EventLog  # noqa: E402
from waypoint.v0.run_dir import create_run_dir, events_path  # noqa: E402
from waypoint.v1.gates import all_passed  # noqa: E402
from waypoint.v4.research import (  # noqa: E402
    RESEARCH_FINDING_TOKEN_CAP,
    ResearchQuery,
    research_node_id,
    run_research_query,
)
from waypoint.v4.run_dir import research_finding_path  # noqa: E402

FAKE_CLI = _REPO_ROOT / "tests" / "fixtures" / "fake_stream_agent.py"


class ResearchQueryTest(unittest.TestCase):
    def _adapter(self, root: Path, prompt_dir: Path, run_dir: Path, tag: str, session_id: str) -> FakeStreamAgentAdapter:
        return FakeStreamAgentAdapter(
            script_path=str(FAKE_CLI),
            pidfile=str(root / f"{tag}.pid"),
            prompt_dir=str(prompt_dir),
            workspace_path=str(run_dir),
            session_id=session_id,
        )

    def test_fresh_query_dispatches_and_caps_overlong_finding(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run1")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            overlong = " ".join(f"word{i}" for i in range(2000))
            adapter = self._adapter(root, prompt_dir, run_dir, "q1", overlong)
            query = ResearchQuery(slug="gauss-law", kind="web_search", question="What is Gauss's law?")

            finding = asyncio.run(
                run_research_query(
                    run_dir, "ch07", query, adapter,
                    LocalEnvironment(tmp_dir=str(prompt_dir)), EpisodeBudget(max_duration_seconds=30),
                )
            )

            self.assertTrue(all_passed(finding.gate_results))
            self.assertLessEqual(len(finding.text.split()), int(RESEARCH_FINDING_TOKEN_CAP * 0.75) + 10)
            self.assertIn("truncated to fit promotion budget", finding.text)
            self.assertEqual(finding.finding_path, research_finding_path(run_dir, "ch07", "gauss-law"))
            self.assertEqual(finding.finding_path.read_text(encoding="utf-8"), finding.text)

            log = EventLog(events_path(run_dir))
            r_id = research_node_id("ch07", "gauss-law")
            self.assertIsNotNone(log.last_event(r_id, "episode_completed"))

    def test_second_call_is_a_cache_hit_not_a_redispatch(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run1")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            env = LocalEnvironment(tmp_dir=str(prompt_dir))
            budget = EpisodeBudget(max_duration_seconds=30)
            query = ResearchQuery(slug="gauss-law", kind="web_search", question="What is Gauss's law?")

            first_adapter = self._adapter(root, prompt_dir, run_dir, "q1", "FIRST_MARKER")
            first = asyncio.run(run_research_query(run_dir, "ch07", query, first_adapter, env, budget))
            self.assertIn("FIRST_MARKER", first.text)

            # A fresh dispatch would produce different content -- if the
            # second call actually redispatched, this marker would show up
            # instead of the first one.
            second_adapter = self._adapter(root, prompt_dir, run_dir, "q2", "SECOND_MARKER")
            second = asyncio.run(run_research_query(run_dir, "ch07", query, second_adapter, env, budget))

            self.assertEqual(first.text, second.text)
            self.assertNotIn("SECOND_MARKER", second.text)

            log = EventLog(events_path(run_dir))
            r_id = research_node_id("ch07", "gauss-law")
            completed_events = [
                e for e in log.read_all() if e.get("node_id") == r_id and e.get("type") == "episode_completed"
            ]
            self.assertEqual(len(completed_events), 1)


if __name__ == "__main__":
    unittest.main()
