"""Codex plugin discovery and explicit setup used by the doctor command."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

COMPUTER_USE_PLUGIN_ID = "computer-use@openai-bundled"


class CodexPluginError(RuntimeError):
    """Raised when a required Codex plugin cannot be made ready."""


@dataclass(frozen=True)
class CodexPluginState:
    plugin_id: str
    installed: bool
    enabled: bool
    available: bool
    version: str = ""

    @property
    def ready(self) -> bool:
        return self.installed and self.enabled


StatusCallback = Callable[[str, str], None]


def install_computer_use_plugin(
    *,
    on_status: StatusCallback | None = None,
    codex_binary: str | None = None,
) -> CodexPluginState:
    """Install and enable Codex Computer Use after explicit doctor opt-in."""

    binary = _resolve_codex_binary(codex_binary)
    state = get_codex_plugin_state(COMPUTER_USE_PLUGIN_ID, codex_binary=binary)
    if state.ready:
        return state
    if not state.available:
        raise CodexPluginError(
            f"Codex does not expose {COMPUTER_USE_PLUGIN_ID}. "
            "Please update Codex CLI or ask your workspace administrator to make the plugin available."
        )

    if not state.installed:
        _notify(on_status, "installing", f"Installing {COMPUTER_USE_PLUGIN_ID}…")
        _install_plugin(binary, COMPUTER_USE_PLUGIN_ID)
        state = get_codex_plugin_state(COMPUTER_USE_PLUGIN_ID, codex_binary=binary)

    if state.installed and not state.enabled:
        _notify(on_status, "enabling", f"Enabling {COMPUTER_USE_PLUGIN_ID}…")
        _enable_plugin_in_user_config(COMPUTER_USE_PLUGIN_ID)
        state = get_codex_plugin_state(COMPUTER_USE_PLUGIN_ID, codex_binary=binary)

    if not state.ready:
        raise CodexPluginError(
            f"{COMPUTER_USE_PLUGIN_ID} is still not enabled after setup. "
            "A project or managed Codex policy may be overriding the user configuration."
        )
    return state


def uninstall_computer_use_plugin(
    *,
    on_status: StatusCallback | None = None,
    codex_binary: str | None = None,
) -> CodexPluginState:
    """Remove Codex Computer Use after explicit doctor opt-in."""

    binary = _resolve_codex_binary(codex_binary)
    state = get_codex_plugin_state(COMPUTER_USE_PLUGIN_ID, codex_binary=binary)
    if not state.installed:
        return state
    _notify(on_status, "removing", f"Removing {COMPUTER_USE_PLUGIN_ID}…")
    _remove_plugin(binary, COMPUTER_USE_PLUGIN_ID)
    state = get_codex_plugin_state(COMPUTER_USE_PLUGIN_ID, codex_binary=binary)
    if state.installed:
        raise CodexPluginError(
            f"{COMPUTER_USE_PLUGIN_ID} is still installed after Codex reported a successful removal."
        )
    return state


def get_codex_plugin_state(
    plugin_id: str,
    *,
    codex_binary: str | None = None,
) -> CodexPluginState:
    """Read one plugin's effective installed/enabled state from Codex JSON."""

    binary = _resolve_codex_binary(codex_binary)
    result = _run_codex(
        [binary, "plugin", "list", "--available", "--json"],
        timeout=30,
        operation="list Codex plugins",
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CodexPluginError("Codex returned invalid JSON while listing plugins.") from exc
    if not isinstance(payload, dict):
        raise CodexPluginError("Codex returned an unexpected plugin-list response.")

    entries: list[dict] = []
    for collection in (payload.get("installed"), payload.get("available")):
        if isinstance(collection, list):
            entries.extend(item for item in collection if isinstance(item, dict))
    entry = next((item for item in entries if item.get("pluginId") == plugin_id), None)
    if entry is None:
        return CodexPluginState(plugin_id, False, False, False)
    return CodexPluginState(
        plugin_id=plugin_id,
        installed=bool(entry.get("installed")),
        enabled=bool(entry.get("enabled")),
        available=True,
        version=str(entry.get("version") or ""),
    )


def _resolve_codex_binary(explicit: str | None) -> str:
    binary = explicit or shutil.which("codex")
    if not binary:
        raise CodexPluginError(
            "Codex CLI was not found. Install Codex and make sure `codex` is available on PATH."
        )
    return binary


def _install_plugin(binary: str, plugin_id: str) -> None:
    _run_codex(
        [binary, "plugin", "add", plugin_id, "--json"],
        timeout=120,
        operation=f"install {plugin_id}",
    )


def _remove_plugin(binary: str, plugin_id: str) -> None:
    _run_codex(
        [binary, "plugin", "remove", plugin_id, "--json"],
        timeout=120,
        operation=f"remove {plugin_id}",
    )


def _run_codex(command: list[str], *, timeout: int, operation: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CodexPluginError("Codex CLI disappeared while running the plugin preflight.") from exc
    except subprocess.TimeoutExpired as exc:
        raise CodexPluginError(f"Timed out while trying to {operation}.") from exc
    except OSError as exc:
        raise CodexPluginError(f"Could not {operation}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if len(detail) > 800:
            detail = detail[-800:]
        suffix = f": {detail}" if detail else ""
        raise CodexPluginError(f"Failed to {operation}{suffix}")
    return result


def _enable_plugin_in_user_config(plugin_id: str) -> None:
    """Set one plugin's user-level enable bit without rewriting other config."""

    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    config_path = codex_home / "config.toml"
    try:
        original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    except OSError as exc:
        raise CodexPluginError(f"Could not read Codex config at {config_path}: {exc}") from exc

    section = f'[plugins."{plugin_id}"]'
    section_match = re.search(rf"(?m)^[ \t]*{re.escape(section)}[ \t]*(?:#.*)?$", original)
    if section_match:
        next_section = re.search(r"(?m)^[ \t]*\[", original[section_match.end() :])
        block_end = (
            section_match.end() + next_section.start()
            if next_section is not None
            else len(original)
        )
        block = original[section_match.end() : block_end]
        enabled_match = re.search(
            r"(?m)^[ \t]*enabled[ \t]*=[ \t]*(?:true|false)[ \t]*(?:#.*)?$",
            block,
        )
        if enabled_match:
            start = section_match.end() + enabled_match.start()
            end = section_match.end() + enabled_match.end()
            updated = original[:start] + "enabled = true" + original[end:]
        else:
            updated = original[: section_match.end()] + "\nenabled = true" + original[section_match.end() :]
    else:
        separator = "" if not original else ("\n" if original.endswith("\n") else "\n\n")
        updated = original + separator + section + "\nenabled = true\n"

    if updated == original:
        return
    try:
        codex_home.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=codex_home,
            prefix="config.toml.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            temp_path = Path(fh.name)
            fh.write(updated)
        if config_path.exists():
            temp_path.chmod(config_path.stat().st_mode & 0o777)
        else:
            temp_path.chmod(0o600)
        os.replace(temp_path, config_path)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except (OSError, UnboundLocalError):
            pass
        raise CodexPluginError(f"Could not enable {plugin_id} in {config_path}: {exc}") from exc


def _notify(callback: StatusCallback | None, status: str, message: str) -> None:
    if callback is not None:
        callback(status, message)
