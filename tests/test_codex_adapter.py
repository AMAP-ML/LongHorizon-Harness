from __future__ import annotations

import shlex
from pathlib import Path

import pytest
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.adapters import codex as codex_adapter_module
from lh_harness.adapters.codex import CodexAdapter
from lh_harness.utils import agent_cli
from lh_harness.utils.agent_cli import (
    is_agent_binary_available,
    resolve_agent_binary,
    resolve_codex_binary,
)
from lh_harness.webapi import server as web_server

from .fake_cli import fake_cli


def _executable(path: Path) -> str:
    """Create a deterministic executable stand-in for a discovered binary.

    Availability is proven by running `--version`, so the stub has to answer it
    like a real CLI; a bare no-op is correctly reported as installed but
    unusable.
    """
    return fake_cli(path, 'print("codex-cli 1.2.3")\n')


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        (
            {
                "LH_HARNESS_CODEX_BINARY": "/custom/LH Codex/codex",
                "CODEX_CLI_PATH": "/custom/CLI Codex/codex",
            },
            "/custom/LH Codex/codex",
        ),
        (
            {"CODEX_CLI_PATH": "/custom/CLI Codex/codex"},
            "/custom/CLI Codex/codex",
        ),
        (
            {
                "LH_HARNESS_CODEX_BINARY": "   ",
                "CODEX_CLI_PATH": "/custom/CLI Codex/codex",
            },
            "/custom/CLI Codex/codex",
        ),
    ],
)
def test_codex_binary_environment_precedence(
    environment: dict[str, str], expected: str
) -> None:
    """Explicit overrides win over the desktop app and PATH discovery."""
    assert resolve_codex_binary(environ=environment, platform_name="darwin") == expected


def test_shared_agent_resolver_applies_codex_policy() -> None:
    selected = resolve_agent_binary(
        "codex",
        environ={"LH_HARNESS_CODEX_BINARY": "/custom/Codex Desktop/codex"},
        platform_name="linux",
    )

    assert selected == "/custom/Codex Desktop/codex"


def test_desktop_codex_binary_wins_over_path(monkeypatch, tmp_path: Path) -> None:
    desktop = _executable(tmp_path / "ChatGPT Desktop.app" / "Contents" / "Resources" / "codex")
    path_binary = _executable(tmp_path / "npm bin" / "codex")

    # The real desktop location is fixed on macOS. Point that location at a
    # temporary executable so this test stays hermetic on machines without the
    # ChatGPT app installed.
    monkeypatch.setattr(agent_cli, "_CODEX_DESKTOP_BINARY", desktop)
    monkeypatch.setattr(agent_cli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(agent_cli.shutil, "which", lambda _name: path_binary)

    assert resolve_codex_binary(environ={}, platform_name="darwin") == desktop


def test_codex_binary_falls_back_to_path_when_desktop_is_missing(
    monkeypatch, tmp_path: Path
) -> None:
    path_binary = _executable(tmp_path / "standalone codex" / "codex")
    monkeypatch.setattr(agent_cli, "_CODEX_DESKTOP_BINARY", str(tmp_path / "missing" / "codex"))
    monkeypatch.setattr(agent_cli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(agent_cli.shutil, "which", lambda _name: path_binary)

    assert resolve_codex_binary(environ={}, platform_name="darwin") == path_binary


def test_codex_adapter_keeps_a_spaced_binary_as_one_argument(monkeypatch, tmp_path: Path) -> None:
    binary = str(tmp_path / "ChatGPT Desktop.app" / "Contents" / "Resources" / "codex")
    monkeypatch.setattr(codex_adapter_module, "resolve_codex_binary", lambda: binary)

    adapter = CodexAdapter(model=None)

    # There is no shell in the agent path any more, so a path with spaces needs
    # no quoting: it simply stays one argv element.
    assert adapter.argv[0] == binary
    assert adapter.argv[1] == "exec"
    assert adapter.argv[-1] == "-"


def test_web_meta_reports_the_resolved_codex_binary(monkeypatch, tmp_path: Path) -> None:
    binary = _executable(tmp_path / "ChatGPT Desktop.app" / "Contents" / "Resources" / "codex")
    monkeypatch.setattr(web_server, "resolve_codex_binary", lambda: binary)

    client = TestClient(web_server.create_app(runs_root=tmp_path / "runs"))
    codex = next(item for item in client.get("/api/meta").json()["agents"] if item["id"] == "codex")

    assert codex["available"] is True
    assert codex["binary"] == binary


def test_web_meta_does_not_claim_missing_explicit_binary(monkeypatch, tmp_path: Path) -> None:
    missing = str(tmp_path / "missing Codex" / "codex")
    monkeypatch.setattr(web_server, "resolve_codex_binary", lambda: missing)

    client = TestClient(web_server.create_app(runs_root=tmp_path / "runs"))
    codex = next(item for item in client.get("/api/meta").json()["agents"] if item["id"] == "codex")

    assert is_agent_binary_available(missing) is False
    assert codex["available"] is False
    assert codex["binary"] == missing
