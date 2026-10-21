"""OpenCode computer-use wiring: plugin config format, state, adapter merge."""

from __future__ import annotations

import json
import posixpath
from pathlib import Path

import pytest

from lh_harness.adapters import opencode as opencode_module
from lh_harness.adapters.opencode import OpenCodeAdapter
from lh_harness.plugins import state as plugin_state
from lh_harness.plugins.community_computer_use import CLAWDCURSOR, OPEN_COMPUTER_USE


@pytest.fixture()
def plugin_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "plugins-home"
    monkeypatch.setattr(plugin_state, "plugins_root", lambda: root)
    return root


def test_community_plugins_declare_opencode_support() -> None:
    for plugin in (OPEN_COMPUTER_USE, CLAWDCURSOR):
        assert plugin.supports("opencode")


def test_write_mcp_config_opencode_native_format(plugin_home: Path) -> None:
    path = plugin_state.write_mcp_config(
        "open-computer-use",
        "opencode",
        server_name="open-computer-use",
        command="open-computer-use",
        args=["mcp"],
    )
    assert path.name == "open-computer-use.opencode.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    server = payload["mcp"]["open-computer-use"]
    assert server == {
        "type": "local",
        "command": "open-computer-use",
        "args": ["mcp"],
        "enabled": True,
    }


def test_active_plugin_for_agent_opencode(plugin_home: Path) -> None:
    config = plugin_state.write_mcp_config(
        "clawdcursor",
        "opencode",
        server_name="clawdcursor",
        command="clawdcursor",
        args=["mcp", "--compact"],
    )
    plugin_state.record_install(
        "clawdcursor",
        agents=["opencode"],
        mcp_configs={"opencode": str(config)},
        mcp_server_name="clawdcursor",
    )
    active = plugin_state.active_plugin_for_agent("opencode")
    assert active == ("clawdcursor", str(config))


def test_adapter_merges_mcp_servers_into_runtime_config(tmp_path: Path) -> None:
    mcp_file = tmp_path / "cu.opencode.json"
    mcp_file.write_text(
        json.dumps(
            {
                "mcp": {
                    "open-computer-use": {
                        "type": "local",
                        "command": "open-computer-use",
                        "args": ["mcp"],
                        "enabled": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    prompt_dir = tmp_path / "run state" / "prompts"
    OpenCodeAdapter(
        model="opencode/mimo-v2.5-free",
        prompt_dir=str(prompt_dir),
        mcp_config=str(mcp_file),
    )

    config_path = posixpath.join(str(prompt_dir), "opencode-runtime-opencode.json")
    payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    assert "open-computer-use" in payload["mcp"]
    assert "provider" not in payload


def test_adapter_merges_base_url_and_mcp_together(tmp_path: Path) -> None:
    mcp_file = tmp_path / "cu.opencode.json"
    mcp_file.write_text(
        json.dumps({"mcp": {"clawdcursor": {"type": "local", "command": "clawdcursor", "enabled": True}}}),
        encoding="utf-8",
    )
    prompt_dir = tmp_path / "prompts"
    OpenCodeAdapter(
        model="apiyi/mimo-x",
        base_url="https://api.example.com/v1/",
        prompt_dir=str(prompt_dir),
        mcp_config=str(mcp_file),
    )

    config_path = posixpath.join(str(prompt_dir), "opencode-runtime-apiyi.json")
    payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    assert payload["provider"]["apiyi"]["options"]["baseURL"] == "https://api.example.com/v1"
    assert "clawdcursor" in payload["mcp"]


def test_adapter_rejects_unreadable_mcp_config(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="could not read"):
        OpenCodeAdapter(
            prompt_dir=str(tmp_path / "prompts"),
            mcp_config=str(tmp_path / "missing.opencode.json"),
        )


def test_adapter_rejects_mcp_config_without_servers(tmp_path: Path) -> None:
    mcp_file = tmp_path / "empty.opencode.json"
    mcp_file.write_text(json.dumps({"mcp": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="holds no `mcp` servers"):
        OpenCodeAdapter(
            prompt_dir=str(tmp_path / "prompts"),
            mcp_config=str(mcp_file),
        )


def test_adapter_without_mcp_and_base_url_writes_no_config(tmp_path: Path) -> None:
    prompt_dir = tmp_path / "prompts"
    OpenCodeAdapter(prompt_dir=str(prompt_dir))
    assert not Path(posixpath.join(str(prompt_dir), "opencode-runtime-opencode.json")).exists()
