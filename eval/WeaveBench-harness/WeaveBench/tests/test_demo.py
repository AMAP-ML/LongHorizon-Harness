"""weavebench-demo CLI smoke."""
from __future__ import annotations

import json
import sys

import pytest

from weavebench.scripts import demo as demo_mod


def test_demo_prints_summary(capsys):
    rc = demo_mod.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "weavebench-demo" in out
    assert "WEB_task_1_mockup_pixel_diff" in out
    assert "claude-opus-4.7" in out
    assert "OVERALL SCORE:" in out
    # Per-clause evidence surfaced
    assert "diffs.json" in out


def test_demo_raw_emits_valid_json(capsys):
    rc = demo_mod.main(["--raw"])
    assert rc == 0
    out = capsys.readouterr().out
    d = json.loads(out)
    assert d["task_id"] == "WEB_task_1_mockup_pixel_diff"
    assert isinstance(d["scores"]["artifact_checks"], list)


def test_demo_unknown_fixture_fails_friendly(capsys):
    rc = demo_mod.main(["--fixture", "does_not_exist"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "fixture not found" in err
    assert "available:" in err
