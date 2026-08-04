"""Smoke tests for weavebench-download-judge placeholder substitution.

Network-free: we hit the substitution logic by faking the download into a
local tarball, so the test exercises the same code path as the real CLI
without needing $HF_TOKEN or a network round-trip.
"""
from __future__ import annotations

import json
import os
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from weavebench.scripts import download_judge


def _make_fake_tarball(tmp_path: Path) -> Path:
    """Build a tarball with the same structure as judge/judge_template.tar.gz."""
    src = tmp_path / "src"
    (src / "profile" / "agents" / "main").mkdir(parents=True)
    (src / "profile" / "agents" / "main" / ".gitkeep").touch()
    (src / "workspace").mkdir(parents=True)
    (src / "workspace" / "AGENTS.md").write_text("# upstream openclaw starter\n")

    openclaw_json = {
        "agents": {"defaults": {"workspace": "PLACEHOLDER_WORKSPACE_DIR"}},
        "gateway": {"auth": {"token": "PLACEHOLDER_GATEWAY_TOKEN"}},
        "models": {"providers": {"openrouter": {
            "apiKey": "PLACEHOLDER_OPENROUTER_API_KEY"}}},
    }
    (src / "profile" / "openclaw.json").write_text(json.dumps(openclaw_json))

    tar_path = tmp_path / "judge_template.tar.gz"
    with tarfile.open(tar_path, "w:gz") as t:
        for p in src.rglob("*"):
            t.add(p, arcname=p.relative_to(src))
    return tar_path


def test_substitutions_with_openrouter_key(tmp_path, monkeypatch):
    tarball = _make_fake_tarball(tmp_path)
    judge_home = tmp_path / "judge_home"

    monkeypatch.setenv("HF_TOKEN", "fake-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-real-key-12345")

    with patch("huggingface_hub.hf_hub_download", return_value=str(tarball)):
        rc = download_judge.main([
            "--judge-home", str(judge_home),
            "--force",
        ])

    assert rc == 0
    d = json.loads((judge_home / "template_profile" / "openclaw.json").read_text())
    assert d["agents"]["defaults"]["workspace"] == str(judge_home / "judge_workspace")
    assert d["models"]["providers"]["openrouter"]["apiKey"] == "sk-or-test-real-key-12345"
    # Random 96-bit gateway token (24 hex bytes × 2 = 48 chars)
    assert len(d["gateway"]["auth"]["token"]) == 48
    assert all(c in "0123456789abcdef" for c in d["gateway"]["auth"]["token"])


def test_substitution_warns_when_openrouter_key_missing(tmp_path, monkeypatch, capsys):
    tarball = _make_fake_tarball(tmp_path)
    judge_home = tmp_path / "judge_home"

    monkeypatch.setenv("HF_TOKEN", "fake-token")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with patch("huggingface_hub.hf_hub_download", return_value=str(tarball)):
        rc = download_judge.main([
            "--judge-home", str(judge_home),
            "--force",
        ])

    assert rc == 0
    err = capsys.readouterr().err
    # The phrase needs to clearly tell the user to edit the file — not just
    # mention OPENROUTER_API_KEY (which appears in the placeholder string too).
    assert "$OPENROUTER_API_KEY not set" in err
    assert "edit it manually" in err
    d = json.loads((judge_home / "template_profile" / "openclaw.json").read_text())
    assert d["models"]["providers"]["openrouter"]["apiKey"] == "PLACEHOLDER_OPENROUTER_API_KEY"


def test_substitution_adversarial_key_containing_placeholder_string(tmp_path, monkeypatch):
    """If $OPENROUTER_API_KEY happens to contain a literal 'PLACEHOLDER_*' substring,
    the substitution must NOT recursively overwrite it on subsequent passes."""
    tarball = _make_fake_tarball(tmp_path)
    judge_home = tmp_path / "judge_home"

    monkeypatch.setenv("HF_TOKEN", "fake-token")
    # adversarial: the user's "key" literally contains PLACEHOLDER_GATEWAY_TOKEN
    adversarial_key = "sk-or-test-PLACEHOLDER_GATEWAY_TOKEN-real-key"
    monkeypatch.setenv("OPENROUTER_API_KEY", adversarial_key)

    with patch("huggingface_hub.hf_hub_download", return_value=str(tarball)):
        rc = download_judge.main(["--judge-home", str(judge_home), "--force"])

    assert rc == 0
    d = json.loads((judge_home / "template_profile" / "openclaw.json").read_text())
    # The key must survive intact — naive ordered str.replace() would have
    # caught the substring on pass 2 and overwritten it with the gateway token.
    assert d["models"]["providers"]["openrouter"]["apiKey"] == adversarial_key
    assert len(d["gateway"]["auth"]["token"]) == 48


def test_refuses_to_overwrite_user_edited_template(tmp_path, monkeypatch, capsys):
    """H2 regression: if the user edited openclaw.json (no PLACEHOLDER markers
    remain), don't clobber it — even without --force the bootstrap should
    no-op and just print the next-step hints."""
    tarball = _make_fake_tarball(tmp_path)
    judge_home = tmp_path / "judge_home"
    (judge_home / "template_profile").mkdir(parents=True)
    user_edited = (judge_home / "template_profile" / "openclaw.json")
    user_edited.write_text(json.dumps({
        "agents": {"defaults": {"workspace": "/custom/workspace"}},
        "gateway": {"auth": {"token": "user-rotated-token"}},
        "models": {"providers": {"openrouter": {
            "apiKey": "sk-or-user-edited-key"}}},
    }))

    monkeypatch.setenv("HF_TOKEN", "fake-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-different-key")

    with patch("huggingface_hub.hf_hub_download", return_value=str(tarball)):
        rc = download_judge.main(["--judge-home", str(judge_home)])

    assert rc == 0  # no-op, not an error
    err = capsys.readouterr().err
    assert "appears to have been edited" in err
    # User's edits MUST survive
    d = json.loads(user_edited.read_text())
    assert d["agents"]["defaults"]["workspace"] == "/custom/workspace"
    assert d["gateway"]["auth"]["token"] == "user-rotated-token"
    assert d["models"]["providers"]["openrouter"]["apiKey"] == "sk-or-user-edited-key"


def test_force_overwrites_user_edited_template(tmp_path, monkeypatch):
    """--force must still let the user fully re-bootstrap."""
    tarball = _make_fake_tarball(tmp_path)
    judge_home = tmp_path / "judge_home"
    (judge_home / "template_profile").mkdir(parents=True)
    (judge_home / "template_profile" / "openclaw.json").write_text(json.dumps({
        "models": {"providers": {"openrouter": {"apiKey": "sk-or-stale-key"}}},
    }))

    monkeypatch.setenv("HF_TOKEN", "fake-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fresh-key")

    with patch("huggingface_hub.hf_hub_download", return_value=str(tarball)):
        rc = download_judge.main(["--judge-home", str(judge_home), "--force"])

    assert rc == 0
    d = json.loads((judge_home / "template_profile" / "openclaw.json").read_text())
    # The freshly-substituted key from the tarball
    assert d["models"]["providers"]["openrouter"]["apiKey"] == "sk-or-fresh-key"
