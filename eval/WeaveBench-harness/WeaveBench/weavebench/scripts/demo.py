"""weavebench-demo — runs the trajectory-aware judge on a checked-in fixture.

This is the no-VM, no-API-key, no-LLM smoke path. Useful when you:
  - just want to see what score.json + per-clause evidence look like
  - are on a laptop without KVM/Docker and can't run the real pipeline
  - want to confirm your `pip install -e .` worked

It loads a real rollout from examples/fixtures/ (a Claude Opus 4.7 attempt
at WEB_task_1_mockup_pixel_diff, judged by gpt-5.5_via_openclaw — the same
trajectory-aware Agent-as-Judge pipeline used in the paper) and prints:

  1. the task spec the agent was given
  2. a summary of what the agent delivered
  3. the per-artifact, per-clause judge scoring (with evidence quotes)
  4. the overall score

No network calls. No tokens needed. Runs in <1 second.

Usage::

    weavebench-demo                  # default fixture
    weavebench-demo --fixture <name> # if more fixtures land later
    weavebench-demo --raw            # dump raw score.json instead of summary
"""
from __future__ import annotations

import argparse
import json
import sys
import tarfile
from pathlib import Path


_HERE = Path(__file__).resolve().parent
_FIXTURES_ROOT = _HERE.parent.parent / "examples" / "fixtures"


def _format_score(s):
    if isinstance(s, bool):
        return "✓ true " if s else "✗ false"
    if s == "partial":
        return "~ partial"
    if isinstance(s, (int, float)):
        return f"  {s:.2f}"
    return str(s)


def _print_summary(score_path: Path, results_tgz: Path) -> None:
    d = json.loads(score_path.read_text())

    print("=" * 72)
    print(f"  weavebench-demo  ·  {d['task_id']}  ·  {d['category']}")
    print("=" * 72)
    print()
    print(f"  model         : {d['model']}")
    print(f"  mode          : {d['mode']}")
    print(f"  judge method  : {d['scores']['judge_method']}")
    print(f"  judge model   : {d['scores']['judge_model']}")
    print(f"  judge elapsed : {d['scores']['judge_elapsed_s']:.1f}s")
    print()

    # What the agent actually delivered
    if results_tgz.exists():
        with tarfile.open(results_tgz) as t:
            files = [m.name for m in t.getmembers() if m.isfile()]
        print(f"  Agent deliverables in results.tar.gz ({results_tgz.stat().st_size // 1024} KB):")
        for f in files[:8]:
            print(f"    - {f}")
        if len(files) > 8:
            print(f"    ... and {len(files) - 8} more")
        print()

    print("-" * 72)
    print("  Per-artifact judge scoring")
    print("-" * 72)
    checks = d["scores"]["artifact_checks"]
    for c in checks:
        status = "✓" if c["exists"] and c["correctness"] >= 0.5 else "✗"
        print(f"\n  {status} {c['id']}   (correctness={c['correctness']:.2f}, exists={c['exists']})")
        # One representative clause result
        for clr in c.get("clause_results", [])[:3]:
            mark = _format_score(clr["satisfied"])
            clause = clr["clause"][:90]
            print(f"      {mark}  {clause}")
            evidence = clr.get("evidence", "")
            if evidence:
                print(f"          ↳ {evidence[:120]}")
        if len(c.get("clause_results", [])) > 3:
            print(f"      ... and {len(c['clause_results']) - 3} more clauses")
        if c.get("missing_or_wrong"):
            print(f"      missing_or_wrong: {c['missing_or_wrong'][:140]}")

    print()
    print("=" * 72)
    score = d.get("score", 0)
    print(f"  OVERALL SCORE: {score:.2f}  ({len(checks)} artifacts evaluated)")
    print("=" * 72)
    print()
    print(f"  Full score.json: {score_path}  ({score_path.stat().st_size} bytes)")
    print()
    print("  To run this for real on your own agent:")
    print("    1. bash scripts/setup.sh     # one-time, needs KVM + OpenRouter key")
    print("    2. weavebench-run --harness openclaw --model openai/gpt-5.5 \\")
    print("           --tasks_root ./cache/tasks --domains WEB --task_filter task_1_ \\")
    print("           --result_dir ./results/smoke")
    print()
    print("  See docs/REPRODUCE.md to match the exact paper numbers.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="weavebench-demo",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    ap.add_argument("--fixture", default="WEB_task_1_mockup_pixel_diff",
                    help="Name of the fixture under examples/fixtures/.")
    ap.add_argument("--raw", action="store_true",
                    help="Print the raw score.json instead of a summary.")
    args = ap.parse_args(argv)

    fixture_dir = _FIXTURES_ROOT / args.fixture
    if not fixture_dir.is_dir():
        print(f"[error] fixture not found: {fixture_dir}", file=sys.stderr)
        print(f"        available: {sorted(p.name for p in _FIXTURES_ROOT.iterdir() if p.is_dir())}",
              file=sys.stderr)
        return 1

    score_path = fixture_dir / "score.json"
    if not score_path.is_file():
        print(f"[error] missing score.json under {fixture_dir}", file=sys.stderr)
        return 2

    if args.raw:
        sys.stdout.write(score_path.read_text())
        return 0

    _print_summary(score_path, fixture_dir / "results.tar.gz")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
