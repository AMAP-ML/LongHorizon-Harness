"""§C5 — the five fixed eval tasks (PLAN.md §C5, line 651).

Each task is a goal plus the canned provider responses a fake model would
have to return for that tier's phases, plus the fixture input (a corpus
string for ``kind="text"`` tasks, a generated workspace tree for
``kind="workspace"`` tasks). The runner (``runner.py``) turns one of these
into a real ``RecursiveDriver`` run.

Call budgets for a first run, per task (derived from the phase machinery,
not asserted here — ``runner.py`` measures them):

- T0: 1  (ESTIMATE only; the direct node's review is free — no judgment
         items, ``v1/reviewer.py`` auto-passes)
- T1: 1  (ESTIMATE only; the one node is built by code, never a plan call)
- T2: 5  (ESTIMATE + PARTITION + 3 windowed document-review passes —
         ``v3/document_review.py``'s ``PASSES=3`` over 1 window, with
         ``keep_depth_pass=False`` per ``driver.py:_phase_review``)
- T3: 2  (ESTIMATE + PARTITION; pilot auto-approves with a blank edit →
         zero contract-derivation calls; T3's review phase spends
         nothing; ``needs_explore=False`` and short plan briefs keep
         probe/research dispatch at zero)

Resume (a second driver against the same run dir) re-runs the
tier-scoped phases only: ``review@T2`` re-runs document review → 3 calls;
T0/T1/T3 resume with zero provider calls (classify/plan/pilot all
short-circuit on their durable artifacts; execute re-runs the round loop
over an already-passed tree, which dispatches nothing).

Every plan brief is deliberately <=7 words with ``prose-dominant`` shape,
so ``v4/probe_planner.py``'s ``needs_probe`` filter (>=8 words, or a
structural shape marker, or an external-lookup marker) admits zero
candidates and no probe-planning call is ever spent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from ..v2.survey import SpineUnit

# Canned ESTIMATE response the classifier turns into T0/T1.
_T0_ESTIMATE: dict = {
    "files_touched": "1",
    "artifacts": 1,
    "answerable_without_exploration": True,
    "ambiguities": [],
    "objections": [],
}

# PASS_VERDICT: a clean document-review window / depth-pass response.
PASS_VERDICT: dict = {"items": [], "verdict": "pass"}


def _corpus_plan(children: list[tuple[str, str]]) -> Callable[[list[SpineUnit]], list[dict]]:
    """A fixed partition tiling one unit per child, by spine order."""

    def plan_for(units: list[SpineUnit]) -> list[dict]:
        return [
            {
                "id": node_id,
                "brief": brief,
                "unit_start": i,
                "unit_end": i,
                "estimated_calls": 3,
                "shape": "prose-dominant",
            }
            for i, (node_id, brief) in enumerate(children)
        ]

    return plan_for


def _workspace_plan(brief_for: Callable[[int], str]) -> Callable[[list[SpineUnit]], list[dict]]:
    """A partition tiling whatever units ``survey_workspace`` actually
    produces — the runner measures the workspace first, so the plan can
    tile it exactly regardless of how the unit count lands."""

    def plan_for(units: list[SpineUnit]) -> list[dict]:
        return [
            {
                "id": f"area-{i}",
                "brief": brief_for(i),
                "unit_start": i,
                "unit_end": i,
                "estimated_calls": 3,
                "shape": "prose-dominant",
            }
            for i in range(len(units))
        ]

    return plan_for


@dataclass(frozen=True)
class EvalTask:
    """One fixed §C5 task: goal, expected tier, canned responses, fixture."""

    task_id: str
    goal: str
    expected_tier: str
    estimate: dict
    plan: Optional[Callable[[list[SpineUnit]], list[dict]]] = None
    # Corpus mode: the source text plus the pre-seeded spine units
    # (id, label, start_chunk, end_chunk, tokens). Pre-seeding means
    # "explore" reuses already-done structure discovery and the model
    # survey never runs (same technique as test_v6_tiering.py's
    # TierOverrideFloorTest).
    corpus: str = ""
    spine_units: tuple[tuple[str, str, int, int, int], ...] = ()
    # Workspace mode: relative path -> file content, written by the runner
    # into a temp work root and measured via measure_workspace.
    workspace: dict[str, str] = field(default_factory=dict)


T0_TYPO_GOAL = "Fix the typo in the docstring on line 12."

T1_NOTES_GOAL = "Turn this single meeting-notes file into a tidy markdown summary."

T2_CORPUS_GOAL = "Write a three-chapter primer on the corpus's subject, one artifact per chapter."

T2_FEATURE_GOAL = "Add a --dry-run flag to the CLI and cover it with a unit test."

T3_REFACTOR_GOAL = (
    "Refactor the module layout across the whole codebase, moving shared "
    "helpers into a common package."
)


def _t2_corpus_text() -> str:
    chapters = [
        ("The basics", "Chapter one of a small corpus: definitions, context, framing."),
        ("The middle", "Chapter two: the core concepts, worked through carefully."),
        ("The end", "Chapter three: advanced topics and open questions."),
    ]
    lines = []
    for title, body in chapters:
        lines.append(f"# {title}\n\n{body}\n\n" + ("Sentence of filler detail. " * 20))
    return "\n".join(lines)


def _meeting_notes() -> str:
    return (
        "# Meeting notes, 2026-08-04\n\n"
        "Attendees: A, B, C.\n\n"
        "Decided: launch the beta in September; the landing page ships first. "
        "Open: budget for the video, who owns the changelog. "
        "Follow-up: B writes the migration checklist before Friday.\n"
    )


def build_tasks() -> tuple[EvalTask, ...]:
    return (
        EvalTask(
            task_id="t0-typo",
            goal=T0_TYPO_GOAL,
            expected_tier="T0",
            estimate=_T0_ESTIMATE,
            workspace={
                "cli.py": (
                    '"""The command line interface.\n\n'
                    "Usage: run the program, pass a file, get output.\n"
                    '"""\n\n'
                    "def main() -> None:\n    print(\"hello wrld\")\n"
                ),
            },
        ),
        EvalTask(
            task_id="t1-notes",
            goal=T1_NOTES_GOAL,
            expected_tier="T1",
            estimate={
                "files_touched": "few",
                "artifacts": 1,
                "answerable_without_exploration": True,
                "ambiguities": [],
                "objections": [],
            },
            corpus=_meeting_notes(),
            spine_units=(("unit-01", "Meeting notes", 0, 0, 320),),
        ),
        EvalTask(
            task_id="t2-corpus",
            goal=T2_CORPUS_GOAL,
            expected_tier="T2",
            estimate={
                "files_touched": "few",
                "artifacts": 3,
                "answerable_without_exploration": True,
                "ambiguities": [],
                "objections": [],
            },
            plan=_corpus_plan(
                [
                    ("intro", "Write the introduction chapter."),
                    ("core", "Write the core concepts chapter."),
                    ("advanced", "Write the advanced topics chapter."),
                ]
            ),
            corpus=_t2_corpus_text(),
            spine_units=(
                ("unit-01", "The basics", 0, 0, 400),
                ("unit-02", "The middle", 1, 1, 500),
                ("unit-03", "The end", 2, 2, 600),
            ),
        ),
        EvalTask(
            task_id="t2-feature",
            goal=T2_FEATURE_GOAL,
            expected_tier="T2",
            estimate={
                "files_touched": "few",
                "artifacts": 2,
                "answerable_without_exploration": True,
                "ambiguities": [],
                "objections": [],
            },
            plan=_corpus_plan(
                [
                    ("cli-flag", "Add the new CLI flag implementation."),
                    ("flag-tests", "Add the unit test for the flag."),
                ]
            ),
            workspace={
                "src/cli.py": (
                    "def run(args):\n"
                    "    # TODO: honor --dry-run\n"
                    "    return 0\n"
                ),
                "tests/test_cli.py": (
                    "def test_run_returns_zero():\n"
                    "    from cli import run\n"
                    "    assert run([]) == 0\n"
                ),
            },
        ),
        EvalTask(
            task_id="t3-refactor",
            goal=T3_REFACTOR_GOAL,
            expected_tier="T3",
            estimate={
                "files_touched": "many",
                "artifacts": 12,
                "answerable_without_exploration": True,
                "ambiguities": [],
                "objections": [],
            },
            plan=_workspace_plan(lambda i: f"Document the refactored layout of area {i}."),
            workspace=_t3_workspace_files(),
        ),
    )


def _t3_workspace_files() -> dict[str, str]:
    """A generated ~600KB+ workspace: 4 top-level packages x 3 modules of
    filler each. The size is load-bearing — T3 classification requires
    ``work_tokens >= 150_000`` (``v6/tiering.py``'s table), so the task
    must genuinely measure that large even with the small-file filler.

    Each file stays far under ``v6/work_object.py``'s 1MB per-file cap,
    so nothing is excluded from measurement as oversized. ``survey_
    workspace`` may split each top-level directory across several units
    (its 8k-token unit ceiling) — the canned plan is generated from the
    *measured* unit list (``_workspace_plan`` tiles ``len(units)``
    children), so the partition stays exact no matter how the
    bin-packing lands.
    """
    filler = (
        "def helper_{n}(x):\n"
        "    \"\"\"Compute a deterministic value from x and {n}.\"\"\"\n"
        "    result = 0\n"
        "    for i in range(x % 17):\n"
        "        result += i * {n}\n"
        "    return result + {n}\n\n"
    )
    files: dict[str, str] = {}
    for pkg in ("core", "io", "util", "web"):
        for module in ("base", "ops", "extras"):
            body = "".join(filler.format(n=n) for n in range(900))
            files[f"{pkg}/{module}.py"] = body
    return files
