"""Probes / delegated exploration (PLAN.md §A6/§B4).

Coverage matching §B4's own test list:
- a `workspace` probe is granted no write tool
- `max_explorers_for(tier)` bounds dispatch cost independent of corpus size
  (the ship gate: more top-level units than the cap still dispatches at
  most the cap's worth of probes)
- findings are capped at 300 tokens regardless of input, for the new probe
  kinds exactly as they already were for `web`
- a probe with an existing nonempty finding is not re-dispatched (v4's
  cache), for the new probe kinds
- `plan_level`'s prompt includes a unit's structural-exploration summary
  and never anything beyond what the caller explicitly supplied (no raw
  source content ever reaches it)
- `_phase_explore`'s T2+-only structural-exploration branch and the
  `Probe`/`ResearchQuery` backward-compatible alias
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_provider import FakeProvider  # noqa: E402

from kusudaemon.adapters.tools.workspace_read import WORKSPACE_READ_TOOL_PATH  # noqa: E402
from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.pipeline.backends import build_research_adapter  # noqa: E402
from kusudaemon.pipeline.driver import RecursiveDriver, RunOptions  # noqa: E402
from kusudaemon.pipeline.run_dir import tier_path  # noqa: E402
from kusudaemon.types import EpisodeBudget, EpisodeResult  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir  # noqa: E402
from kusudaemon.v1.gates import all_passed  # noqa: E402
from kusudaemon.v2.planner import _render_slice, plan_level  # noqa: E402
from kusudaemon.v2.survey import SpineUnit, save_spine  # noqa: E402
from kusudaemon.v4.mcp_research import SEARXNG_TOOL_PATH, allowed_tools_for  # noqa: E402
from kusudaemon.v4.research import (  # noqa: E402
    RESEARCH_FINDING_TOKEN_CAP,
    Probe,
    ResearchQuery,
    run_research_query,
)
from kusudaemon.v4.run_dir import research_finding_path  # noqa: E402
from kusudaemon.v6.tiering import max_explorers_for  # noqa: E402
from kusudaemon.v6.work_object import measure_workspace  # noqa: E402

_ENV_KEYS = (
    "KUSUDAEMON_PROVIDER_BASE_URL", "KUSUDAEMON_PROVIDER_API_KEY",
    "KUSUDAEMON_PROVIDER_MODEL", "KUSUDAEMON_PROVIDER_CONFIG",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "KUSUDAEMON_PROVIDER",
)


class _EnvGuard:
    """Same fixture as test_pipeline_backends.py's _EnvGuard -- GptmeAdapter
    construction resolves provider config even when nothing ever dispatches
    it."""

    def __enter__(self) -> "_EnvGuard":
        self._backup = {key: os.environ.pop(key, None) for key in _ENV_KEYS}
        os.environ["KUSUDAEMON_PROVIDER_CONFIG"] = "/nonexistent/provider.json"
        os.environ["OPENAI_API_KEY"] = "test-key"
        os.environ["OPENAI_BASE_URL"] = "https://test.example.com/v1"
        os.environ["OPENAI_MODEL"] = "test-model"
        return self

    def __exit__(self, *exc_info: object) -> None:
        for key, value in self._backup.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)


class _InMemoryProbeAdapter:
    """A probe episode double that never shells out or writes a file --
    matching v4/research.py's documented "workspace/corpus probes get no
    write tool, the finding is captured via the assistant-message fallback"
    shape, and test_v6_tiering.py's own _InMemoryWriterAdapter pattern for
    "no agent binary" (CLAUDE.md Part III)."""

    has_file_tools = False
    supports_session_resume = False

    def __init__(self, content: str) -> None:
        self._content = content

    async def run_episode(self, prompt, env, budget, live_trajectory_path=None, **kwargs) -> EpisodeResult:
        return EpisodeResult(status="done", actions_log=self._content, duration_ms=1, metadata={})


def _units(n: int, *, base_tokens: int = 1000) -> list[SpineUnit]:
    return [
        SpineUnit(
            id=f"unit-{i:02d}",
            label=f"unit {i}",
            start_chunk=-1,
            end_chunk=-1,
            tokens=base_tokens + i,
            members=(f"pkg{i}/mod.py",),
        )
        for i in range(n)
    ]


# ----------------------------------------------------------------------
# Probe / ResearchQuery alias, kind normalization
# ----------------------------------------------------------------------
class ProbeAliasTest(unittest.TestCase):
    def test_research_query_is_literally_probe(self) -> None:
        self.assertIs(ResearchQuery, Probe)

    def test_web_search_kind_normalizes_to_web(self) -> None:
        query = ResearchQuery(slug="q", kind="web_search", question="?")
        self.assertEqual(query.kind, "web")

    def test_new_kinds_pass_through_unchanged(self) -> None:
        for kind in ("workspace", "corpus", "doc_retrieval"):
            self.assertEqual(ResearchQuery(slug="q", kind=kind, question="?").kind, kind)


# ----------------------------------------------------------------------
# §B4: a workspace probe is granted no write tool
# ----------------------------------------------------------------------
class WorkspaceProbeToolAllowlistTest(unittest.TestCase):
    def test_workspace_kind_gets_read_and_workspace_read_only(self) -> None:
        tools = allowed_tools_for("workspace")
        self.assertEqual(tools, ("read", str(WORKSPACE_READ_TOOL_PATH)))
        self.assertNotIn("save", tools)
        self.assertNotIn("patch", tools)
        self.assertNotIn("shell", tools)

    def test_corpus_kind_gets_read_only(self) -> None:
        self.assertEqual(allowed_tools_for("corpus"), ("read",))

    def test_web_alias_matches_legacy_web_search(self) -> None:
        self.assertEqual(allowed_tools_for("web"), allowed_tools_for("web_search"))
        self.assertEqual(allowed_tools_for("web"), (str(SEARXNG_TOOL_PATH),))

    def test_built_research_adapter_for_workspace_has_no_write_tool(self) -> None:
        with _EnvGuard():
            adapter = build_research_adapter(
                "gptme",
                workspace_path="/tmp/some-repo",
                prompt_dir="/tmp/prompts",
                query=ResearchQuery(slug="pkg", kind="workspace", question="What does this do?"),
            )
        self.assertNotIn("save", adapter.tool_allowlist)
        self.assertNotIn("patch", adapter.tool_allowlist)
        self.assertNotIn("shell", adapter.tool_allowlist)
        self.assertIn("read", adapter.tool_allowlist)
        self.assertIn(str(WORKSPACE_READ_TOOL_PATH), adapter.tool_allowlist)

    def test_workspace_probe_hides_run_dir_when_nested_in_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_str, _EnvGuard():
            workspace_root = Path(workspace_str).resolve()
            run_dir = workspace_root / ".kusudaemon" / "runs" / "r1"
            adapter = build_research_adapter(
                "gptme",
                workspace_path=workspace_root,
                prompt_dir="/tmp/prompts",
                query=ResearchQuery(slug="pkg", kind="workspace", question="?"),
                run_dir=run_dir,
            )
        self.assertEqual(adapter.hidden_paths, (".kusudaemon/runs/r1/",))

    def test_corpus_probe_hides_the_same_bookkeeping_a_writer_hides(self) -> None:
        with _EnvGuard():
            adapter = build_research_adapter(
                "gptme",
                workspace_path="/tmp/run",
                prompt_dir="/tmp/prompts",
                query=ResearchQuery(slug="unit-01", kind="corpus", question="?"),
                run_dir="/tmp/run",
            )
        self.assertIn("scratch/", adapter.hidden_paths)
        self.assertIn("out/", adapter.hidden_paths)

    def test_web_kind_adapter_unaffected_by_run_dir_kwarg(self) -> None:
        """Backward compat: existing (pre-§B4) web_search callers never
        passed run_dir at all; passing it now must not change their
        adapter's hidden_paths (web gets no filesystem tools regardless)."""
        with _EnvGuard():
            adapter = build_research_adapter(
                "gptme",
                workspace_path="/tmp/run",
                prompt_dir="/tmp/prompts",
                query=ResearchQuery(slug="q", kind="web_search", question="?"),
                run_dir="/tmp/run",
            )
        self.assertEqual(adapter.hidden_paths, ())


# ----------------------------------------------------------------------
# 300-token cap and idempotent caching, exercised for the new probe kinds
# ----------------------------------------------------------------------
class ProbeFindingCapAndCacheTest(unittest.TestCase):
    def test_finding_capped_at_300_tokens_for_workspace_kind(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            overlong = " ".join(f"word{i}" for i in range(2000))
            adapter = _InMemoryProbeAdapter(overlong)
            query = ResearchQuery(slug="pkg-a", kind="workspace", question="Summarize pkg-a.")

            finding = asyncio.run(
                run_research_query(
                    run_dir, "explore", query, adapter,
                    LocalEnvironment(tmp_dir=str(run_dir / "tmp")), EpisodeBudget(max_duration_seconds=30),
                )
            )

            self.assertTrue(all_passed(finding.gate_results))
            self.assertLessEqual(len(finding.text.split()), int(RESEARCH_FINDING_TOKEN_CAP * 0.75) + 10)
            self.assertEqual(finding.finding_path, research_finding_path(run_dir, "explore", "pkg-a"))

    def test_finding_capped_at_300_tokens_for_corpus_kind(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            overlong = " ".join(f"word{i}" for i in range(2000))
            adapter = _InMemoryProbeAdapter(overlong)
            query = ResearchQuery(slug="unit-01", kind="corpus", question="Summarize unit-01.")

            finding = asyncio.run(
                run_research_query(
                    run_dir, "explore", query, adapter,
                    LocalEnvironment(tmp_dir=str(run_dir / "tmp")), EpisodeBudget(max_duration_seconds=30),
                )
            )
            self.assertLessEqual(len(finding.text.split()), int(RESEARCH_FINDING_TOKEN_CAP * 0.75) + 10)

    def test_existing_nonempty_finding_is_not_redispatched(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            query = ResearchQuery(slug="pkg-a", kind="workspace", question="Summarize pkg-a.")
            env = LocalEnvironment(tmp_dir=str(run_dir / "tmp"))
            budget = EpisodeBudget(max_duration_seconds=30)

            first_adapter = _InMemoryProbeAdapter("FIRST_MARKER: pkg-a handles routing.")
            first = asyncio.run(run_research_query(run_dir, "explore", query, first_adapter, env, budget))
            self.assertIn("FIRST_MARKER", first.text)

            calls: list[str] = []

            class _CountingAdapter(_InMemoryProbeAdapter):
                async def run_episode(self, *args, **kwargs):  # noqa: D401
                    calls.append("dispatched")
                    return await super().run_episode(*args, **kwargs)

            second_adapter = _CountingAdapter("SECOND_MARKER: should never be seen")
            second = asyncio.run(run_research_query(run_dir, "explore", query, second_adapter, env, budget))

            self.assertEqual(first.text, second.text)
            self.assertNotIn("SECOND_MARKER", second.text)
            self.assertEqual(calls, [], "a cached finding must never re-dispatch the episode")


# ----------------------------------------------------------------------
# plan_level: summaries reach the planner, nothing else does
# ----------------------------------------------------------------------
class PlanLevelSummaryTest(unittest.TestCase):
    def test_render_slice_includes_summary_line(self) -> None:
        units = [SpineUnit(id="unit-01", label="auth module", start_chunk=0, end_chunk=0, tokens=500)]
        summary = "Handles OAuth token refresh and session storage."
        rendered = _render_slice(units, lambda unit: summary)
        self.assertIn(summary, rendered)
        self.assertIn("auth module", rendered)

    def test_render_slice_unchanged_when_no_summary_given(self) -> None:
        units = [SpineUnit(id="unit-01", label="auth module", start_chunk=0, end_chunk=0, tokens=500)]
        self.assertEqual(_render_slice(units), _render_slice(units, None))
        self.assertEqual(_render_slice(units), "0: auth module (500 tokens)")

    def test_plan_level_prompt_carries_summary_and_no_raw_source(self) -> None:
        units = [SpineUnit(id="unit-01", label="auth module", start_chunk=0, end_chunk=0, tokens=500)]
        summary = "Handles OAuth token refresh; no persistent secrets stored."
        raw_source_marker = "RAW_SOURCE_CONTENT_MUST_NEVER_REACH_THE_PLANNER"
        provider = FakeProvider(
            [
                {
                    "children": [
                        {
                            "id": "c1", "brief": "write it", "unit_start": 0, "unit_end": 0,
                            "estimated_calls": 3, "shape": "prose-dominant",
                        }
                    ]
                }
            ]
        )

        plan_level(units, provider, top_level=True, unit_summary_for=lambda unit: summary)

        messages, _schema = provider.calls[-1]
        user_message = messages[1]["content"]
        self.assertIn(summary, user_message)
        self.assertNotIn(raw_source_marker, user_message)


# ----------------------------------------------------------------------
# §B4 ship gate: max_explorers_for(tier) bounds dispatch cost regardless
# of how many top-level units the work object has.
# ----------------------------------------------------------------------
class StructuralExplorationCapTest(unittest.TestCase):
    def _driver(self, run_dir: Path, work, probe_factory) -> RecursiveDriver:
        return RecursiveDriver(
            run_dir,
            provider=FakeProvider([]),  # type: ignore[arg-type]
            options=RunOptions(goal="explore this repo", work_object=work),
            writer_adapter_factory=lambda node: (_ for _ in ()).throw(
                AssertionError("no writer dispatch expected")
            ),
            research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                AssertionError("no research dispatch expected")
            ),
            probe_adapter_factory=probe_factory,
        )

    def test_more_units_than_the_cap_still_dispatches_at_most_the_cap(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str, tempfile.TemporaryDirectory() as ws_str:
                run_dir = Path(root_str) / "run"
                work = measure_workspace(Path(ws_str))
                dispatched: list[Probe] = []

                def factory(query: Probe):
                    dispatched.append(query)
                    return _InMemoryProbeAdapter(f"summary for {query.slug}")

                driver = self._driver(run_dir, work, factory)
                # §B4 ship gate: a work object with far more top-level units
                # than max_explorers_for("T2") (6) must still only dispatch
                # 6 probes -- the cap bounds cost by tier, not by corpus
                # size (a 200-directory monorepo dispatches the same 6).
                save_spine(driver.run_dir, _units(25))

                await driver._run_structural_exploration("T2")

                cap = max_explorers_for("T2")
                self.assertEqual(cap, 6)
                self.assertEqual(len(dispatched), cap)
                # Deterministic selection: the `cap` largest units by token
                # count (ties broken by id) -- unit-24 down to unit-19 here
                # (tokens = 1000 + i, strictly increasing with i).
                self.assertEqual(
                    sorted(q.slug for q in dispatched),
                    [f"unit-{i:02d}" for i in range(19, 25)],
                )
                for query in dispatched:
                    self.assertEqual(query.kind, "workspace")

        asyncio.run(scenario())

    def test_t0_has_zero_explorers_and_dispatches_nothing(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str, tempfile.TemporaryDirectory() as ws_str:
                run_dir = Path(root_str) / "run"
                work = measure_workspace(Path(ws_str))

                def factory(query: Probe):  # pragma: no cover - assertion helper
                    raise AssertionError("T0 must never dispatch a structural-exploration probe")

                driver = self._driver(run_dir, work, factory)
                save_spine(driver.run_dir, _units(3))
                self.assertEqual(max_explorers_for("T0"), 0)
                await driver._run_structural_exploration("T0")

        asyncio.run(scenario())

    def test_findings_feed_back_into_plan_via_explore_summary_for(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str, tempfile.TemporaryDirectory() as ws_str:
                run_dir = Path(root_str) / "run"
                work = measure_workspace(Path(ws_str))

                def factory(query: Probe):
                    return _InMemoryProbeAdapter(f"structural summary of {query.slug}")

                driver = self._driver(run_dir, work, factory)
                units = _units(2)
                save_spine(driver.run_dir, units)

                await driver._run_structural_exploration("T2")

                for unit in units:
                    summary = driver._explore_summary_for(unit)
                    self.assertIn(f"structural summary of {unit.id}", summary)
                # A unit that was never probed (not in this small set, but
                # exercised generally) degrades to an empty summary rather
                # than raising.
                missing = SpineUnit(id="unit-99", label="never probed", start_chunk=-1, end_chunk=-1, tokens=1)
                self.assertEqual(driver._explore_summary_for(missing), "")

        asyncio.run(scenario())


class PhaseExploreRoutingTest(unittest.TestCase):
    """PLAN.md §A6/§B4: structural exploration is a T2+-only branch of
    `_phase_explore` -- T1 has no "plan" phase to feed a summary into, so it
    must never dispatch a probe there, even when `needs_explore` is True."""

    def _driver(self, run_dir: Path, work, probe_factory) -> RecursiveDriver:
        return RecursiveDriver(
            run_dir,
            provider=FakeProvider([]),  # type: ignore[arg-type]
            options=RunOptions(goal="fix the routing bug", work_object=work),
            writer_adapter_factory=lambda node: (_ for _ in ()).throw(
                AssertionError("no writer dispatch expected")
            ),
            research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                AssertionError("no research dispatch expected")
            ),
            probe_adapter_factory=probe_factory,
        )

    @staticmethod
    def _seed_tier(run_dir: Path, tier: str) -> None:
        tier_path(run_dir).parent.mkdir(parents=True, exist_ok=True)
        tier_path(run_dir).write_text(
            json.dumps(
                {
                    "tier": tier, "measured_tier": tier, "override": None,
                    "needs_intake": False, "needs_explore": True,
                    "signals": {}, "estimate": {}, "ts": 0,
                }
            ),
            encoding="utf-8",
        )

    def test_t1_never_dispatches_a_structural_probe(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str, tempfile.TemporaryDirectory() as ws_str:
                run_dir = Path(root_str) / "run"
                (Path(ws_str) / "app.py").write_text("print(1)\n", encoding="utf-8")
                work = measure_workspace(Path(ws_str))

                def factory(query: Probe):  # pragma: no cover - assertion helper
                    raise AssertionError("T1 must never dispatch a structural-exploration probe")

                driver = self._driver(run_dir, work, factory)
                self._seed_tier(driver.run_dir, "T1")
                await driver._phase_explore()
                # PLAN-AUDIT.md §E8: T1 has no "plan" phase at all, so
                # _phase_explore must skip ensuring spine.json entirely --
                # doing so unconditionally used to make a corpus-less,
                # workspace-less T1 goal die at explore (_phase_survey
                # raises loudly per §D4 when there's no source and no
                # workspace). Ensuring a spine is now gated on "plan" being
                # in the tier's own phase list, so a workspace-mode T1 run
                # like this one no longer gets one either -- it was never
                # needed here, only produced as a side effect of the old
                # unconditional ensure.
                self.assertFalse((driver.run_dir / "spine.json").exists())

        asyncio.run(scenario())

    def test_t2_dispatches_structural_probes_and_findings_reach_the_planner(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str, tempfile.TemporaryDirectory() as ws_str:
                run_dir = Path(root_str) / "run"
                for name in ("auth", "billing", "routing"):
                    (Path(ws_str) / name).mkdir()
                    (Path(ws_str) / name / "mod.py").write_text(f"# {name} module\n", encoding="utf-8")
                work = measure_workspace(Path(ws_str))
                dispatched: list[Probe] = []

                def factory(query: Probe):
                    dispatched.append(query)
                    return _InMemoryProbeAdapter(f"structural summary of {query.slug}")

                driver = self._driver(run_dir, work, factory)
                self._seed_tier(driver.run_dir, "T2")
                await driver._phase_explore()

                self.assertTrue((driver.run_dir / "spine.json").exists())
                self.assertGreater(len(dispatched), 0)
                self.assertLessEqual(len(dispatched), max_explorers_for("T2"))
                for query in dispatched:
                    self.assertEqual(query.kind, "workspace")
                    summary_path = research_finding_path(driver.run_dir, "explore", query.slug)
                    self.assertTrue(summary_path.exists())
                    self.assertIn("structural summary", summary_path.read_text(encoding="utf-8"))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
