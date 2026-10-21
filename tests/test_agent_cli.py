from __future__ import annotations

from pathlib import Path

from lh_harness.utils.agent_cli import probe_agent_cli


def _gbk_stub(path: Path) -> str:
    """A fake agent that prints a GBK-encoded version banner.

    ``\\264\\362\\323\\241`` are GBK bytes (invalid as UTF-8) that a zh-CN
    Windows agent CLI may emit; a text-mode reader must not crash on them.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"#!/bin/sh\nprintf '\\264\\362\\323\\241 1.2.3\\n'\n")
    path.chmod(0o755)
    return str(path)


def test_probe_agent_cli_tolerates_non_utf8_version_output(tmp_path: Path) -> None:
    """A GBK banner must not crash the --version probe or hide the version."""
    result = probe_agent_cli("codex", path=_gbk_stub(tmp_path / "codex"))

    assert result.usable
    assert result.version == "1.2.3"
