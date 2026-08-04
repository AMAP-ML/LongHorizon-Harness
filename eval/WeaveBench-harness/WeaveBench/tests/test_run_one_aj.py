"""Tests for the agent-as-judge subdir + bench-root resolution."""
from __future__ import annotations

from pathlib import Path

from weavebench.eval.agent_judge.run_one_aj import (
    _resolve_bench_root,
    _resolve_bench_subdir,
    _parse_agent_log_markers,
    _classify_agent_status,
)
from weavebench.eval.agent_judge.stage_case import task_md_path


def test_default_is_public_layout():
    """Tasks that don't carry an internal bench_subdir get '.' (public layout)."""
    assert _resolve_bench_subdir({}) == "."
    assert _resolve_bench_subdir({"task_id": "WEB_task_1"}) == "."


def test_task_with_explicit_subdir_preserved():
    assert _resolve_bench_subdir({"bench_subdir": "batch_alt"}) == "batch_alt"


def test_empty_string_subdir_falls_back_to_dot():
    assert _resolve_bench_subdir({"bench_subdir": ""}) == "."


def test_bench_root_matches_tasks_root(monkeypatch):
    """Regression for H1: judge must look for tasks at the same dir users pass
    via --tasks_root. Previously _resolve_bench_root appended a stray "tasks"
    segment, so a user running --tasks_root ./cache/tasks made the judge
    search ./cache/tasks/tasks/ — which doesn't exist, silently degrading
    every score.json."""
    monkeypatch.delenv("AJ_BENCH_ROOT", raising=False)
    assert _resolve_bench_root(Path("./cache/tasks")) == Path("./cache/tasks")


def test_bench_root_env_override(monkeypatch):
    monkeypatch.setenv("AJ_BENCH_ROOT", "/custom/path/to/tasks")
    assert _resolve_bench_root(Path("./ignored")) == Path("/custom/path/to/tasks")


def test_full_judge_path_construction(tmp_path, monkeypatch):
    """End-to-end: a user runs --tasks_root <cache>/tasks with no bench_subdir.
    The judge must construct <cache>/tasks/WEB/WEB_task_1_foo.md, which
    *exists* in the public layout, not <cache>/tasks/tasks/...
    """
    monkeypatch.delenv("AJ_BENCH_ROOT", raising=False)
    tasks_root = tmp_path / "tasks"
    (tasks_root / "WEB").mkdir(parents=True)
    (tasks_root / "WEB" / "WEB_task_1_foo.md").write_text("# stub")

    bench_root = _resolve_bench_root(tasks_root)
    subdir = _resolve_bench_subdir({})
    md = task_md_path(bench_root, subdir, "WEB", "WEB_task_1_foo")
    assert md.exists(), f"judge would look at {md}, which doesn't exist"


# --- agent.log marker parsing (shared by all four harnesses) ---------------

def test_parse_markers_missing_file(tmp_path):
    """No agent.log → both markers None (e.g. wall-clock kill before fetch)."""
    out = _parse_agent_log_markers(tmp_path / "nope.log")
    assert out == {"agent_exit": None, "retries_used": None}


def test_parse_markers_clean_exit(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text("...lots of output...\nRETRIES_USED=0\nAGENT_EXIT=0\nDONE\n")
    out = _parse_agent_log_markers(log)
    assert out == {"agent_exit": 0, "retries_used": 0}


def test_parse_markers_nonzero_exit_with_retries(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text("boom\nRETRIES_USED=3\nAGENT_EXIT=1\n")
    out = _parse_agent_log_markers(log)
    assert out == {"agent_exit": 1, "retries_used": 3}


# --- unified health classifier (the core of the four-harness alignment) ----

def _tu(ok=True, n_calls=5, output=2000):
    return {"ok": ok, "n_calls": n_calls, "output": output}


def test_classify_ok_real_output():
    v = _classify_agent_status(True, {"agent_exit": 0, "retries_used": 0}, _tu())
    assert v["status"] == "ok"
    assert v["reason"] is None


def test_classify_timeout_when_not_done():
    """agent_done False → timeout regardless of markers/tokens."""
    v = _classify_agent_status(False, {"agent_exit": None, "retries_used": None},
                               _tu(n_calls=0, output=0))
    assert v["status"] == "timeout"


def test_classify_crashed_on_nonzero_exit():
    v = _classify_agent_status(True, {"agent_exit": 137, "retries_used": 1}, _tu())
    assert v["status"] == "crashed"
    assert "137" in v["reason"]


def test_classify_empty_run_retry_exhausted():
    """THE bug: clean process signals (done True, exit 0) but no model output —
    retry-exhausted upstream failure must be caught as empty_run, not ok."""
    v = _classify_agent_status(
        True, {"agent_exit": 0, "retries_used": 3}, _tu(n_calls=0, output=0))
    assert v["status"] == "empty_run"
    assert "retries_used=3" in v["reason"]


def test_classify_empty_run_bare_refusal():
    """A bare refusal emits a few dozen tokens but no real work → still empty."""
    v = _classify_agent_status(
        True, {"agent_exit": 0, "retries_used": 0}, _tu(n_calls=1, output=5))
    assert v["status"] == "empty_run"


def test_classify_crashed_takes_precedence_over_empty():
    """Non-zero exit is reported as crashed even when tokens are also ~0."""
    v = _classify_agent_status(
        True, {"agent_exit": 1, "retries_used": 2}, _tu(n_calls=0, output=0))
    assert v["status"] == "crashed"


def test_classify_ok_when_token_usage_unavailable():
    """If token aggregation failed (ok=False) we cannot prove emptiness, so a
    clean-exit run is NOT downgraded to empty_run on missing evidence."""
    v = _classify_agent_status(
        True, {"agent_exit": 0, "retries_used": 0},
        {"ok": False, "error": "chat.jsonl missing"})
    assert v["status"] == "ok"
