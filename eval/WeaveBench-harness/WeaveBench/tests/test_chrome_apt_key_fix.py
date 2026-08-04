"""Regression test for the Google Chrome apt signing-key repair.

The published Ubuntu.qcow2 ships a stale
``/etc/apt/sources.list.d/google-chrome.list`` whose signing key Google has
since rotated, so ``apt-get update`` fails with ``NO_PUBKEY FD533C07C264648F``
and any warmup running under ``set -e`` aborts (e.g.
``DAV_task_10_polars_streamlit_xfilter``).

``write_task_env_script`` is the per-task VM-env hook that runs before warmup,
so the fix is invoked there. These tests mock the VM I/O helpers and assert
(a) the fix is wired into that hook and (b) the injected script is correct,
without needing a booted VM.
"""
from __future__ import annotations

from unittest import mock

import weavebench.eval.orchestrator as orch


def _capture_uploads(monkeypatch):
    """Patch the VM I/O helpers and return (uploads, execs) capture lists."""
    uploads: list[tuple[str, str]] = []  # (content, remote_path)
    execs: list[list[str]] = []

    def fake_upload(env, content, remote_path, *a, **k):
        uploads.append((content, remote_path))

    def fake_exec(env, argv, *a, **k):
        execs.append(argv)
        return {"returncode": 0, "output": ""}

    monkeypatch.setattr(orch, "_vm_upload", fake_upload)
    monkeypatch.setattr(orch, "_vm_exec", fake_exec)
    return uploads, execs


def test_fix_is_invoked_from_task_env_hook(monkeypatch):
    """write_task_env_script must call the chrome-key fix every task."""
    called = {}

    def fake_fix(env, http_proxy="", client_password="password"):
        called["http_proxy"] = http_proxy

    monkeypatch.setattr(orch, "_vm_upload", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_vm_exec", lambda *a, **k: {"returncode": 0})
    monkeypatch.setattr(orch, "_fix_google_chrome_apt_key", fake_fix)

    orch.write_task_env_script(mock.MagicMock(), env_block="",
                               http_proxy="http://172.17.0.1:7890")

    assert called.get("http_proxy") == "http://172.17.0.1:7890"


def test_injected_script_refreshes_then_disables(monkeypatch):
    """The uploaded script must try a key refresh, fall back to disabling the
    source, be idempotent, and only touch the chrome list."""
    uploads, execs = _capture_uploads(monkeypatch)

    orch._fix_google_chrome_apt_key(mock.MagicMock(), http_proxy="")

    scripts = [c for c, p in uploads if p.endswith("fix_chrome_key.sh")]
    assert len(scripts) == 1, "expected exactly one chrome-fix script upload"
    s = scripts[0]

    # Only the Chrome source is touched.
    assert "/etc/apt/sources.list.d/google-chrome.list" in s
    # Path 1: refresh the key into a dedicated keyring + signed-by rewrite.
    assert "dl.google.com/linux/linux_signing_key.pub" in s
    assert "/usr/share/keyrings/google-chrome.gpg" in s
    assert "signed-by=" in s
    # Path 2: disable the source if the refresh fails.
    assert ".disabled" in s
    # Idempotent re-entry guard.
    assert "_weavebench_chrome_key_fixed" in s
    # And the script is actually executed in the VM.
    assert any("fix_chrome_key.sh" in " ".join(a) for a in execs)


def test_proxy_export_only_when_proxy_set(monkeypatch):
    """The download must be routed through http_proxy when one is provided,
    and must NOT export an empty proxy otherwise."""
    uploads_with, _ = _capture_uploads(monkeypatch)
    orch._fix_google_chrome_apt_key(mock.MagicMock(),
                                    http_proxy="http://172.17.0.1:7890")
    s_with = [c for c, p in uploads_with if p.endswith("fix_chrome_key.sh")][0]
    assert "http://172.17.0.1:7890" in s_with
    assert "export https_proxy=" in s_with

    uploads_without, _ = _capture_uploads(monkeypatch)
    orch._fix_google_chrome_apt_key(mock.MagicMock(), http_proxy="")
    s_without = [c for c, p in uploads_without
                 if p.endswith("fix_chrome_key.sh")][0]
    assert "export https_proxy=" not in s_without


def test_fix_never_raises(monkeypatch):
    """A failing VM exec must not propagate — task startup must never break."""
    monkeypatch.setattr(orch, "_vm_upload", lambda *a, **k: None)

    def boom(*a, **k):
        raise RuntimeError("vm exec failed")

    monkeypatch.setattr(orch, "_vm_exec", boom)

    # Must swallow the VM transport error rather than crash the task.
    orch._fix_google_chrome_apt_key(mock.MagicMock(), http_proxy="")
