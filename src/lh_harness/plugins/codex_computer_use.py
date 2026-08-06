"""Codex plugin discovery and explicit setup used by the `plugin` command.

This is the one plugin the harness cannot keep entirely inside
`~/.lh-harness/`: Codex loads it from its own plugin registry, so `codex plugin
add` writes `[plugins."<id>"]` into `~/.codex/config.toml` itself. There is no
per-run override for it -- `-c plugins.<id>.enabled=true` is ignored -- so
installing it does grant GUI control to other Codex sessions. The harness only
ever delegates to `codex plugin`; it never edits that file.

Which platforms it runs on is Codex's call, not ours: the marketplace omits the
plugin, or marks it unavailable, where it cannot run. Only the post-install
activation is platform-specific, because macOS gates GUI control behind TCC
grants while Windows gates it behind session and integrity level.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..utils.agent_cli import probe_agent_cli
from .codex_config import codex_config_path
from .errors import PluginError

# Stable harness-facing id, defined in `state` so the priority list can name it
# without importing this module; the vendor id below carries an `@`.
from .state import CODEX_GUI_PLUGIN_ID, forget_install, record_install

COMPUTER_USE_PLUGIN_ID = "computer-use@openai-bundled"

# Kept as an alias so existing callers and error handling keep working.
CodexPluginError = PluginError


@dataclass(frozen=True)
class CodexPluginState:
    plugin_id: str
    installed: bool
    enabled: bool
    available: bool
    version: str = ""
    # Where Codex unpacked the plugin, straight from its own JSON.
    source_path: str = ""

    @property
    def ready(self) -> bool:
        return self.installed and self.enabled


StatusCallback = Callable[[str, str], None]


def install_computer_use_plugin(
    *,
    on_status: StatusCallback | None = None,
    codex_binary: str | None = None,
    activate: bool = True,
) -> CodexPluginState:
    """Install and enable Codex Computer Use after an explicit opt-in.

    Platform support is whatever the local Codex build offers: the marketplace
    omits the plugin, or marks it unavailable, where it cannot run.
    """

    binary = _resolve_codex_binary(codex_binary)
    state = get_codex_plugin_state(COMPUTER_USE_PLUGIN_ID, codex_binary=binary)
    if state.ready:
        # Re-running install is how a user retries the grants, so still activate.
        if activate:
            _activate_runtime(on_status=on_status)
        _record()
        return state
    if not state.available:
        raise CodexPluginError(
            f"Codex does not expose {COMPUTER_USE_PLUGIN_ID}. "
            "Please update Codex CLI or ask your workspace administrator to make the plugin available."
        )

    if not state.installed:
        _notify(
            on_status,
            "note",
            f"Codex owns this plugin's registry, so `codex plugin add` records it in "
            f"{codex_config_path()}. Unlike the npm plugins, it therefore stays available to "
            "your other Codex sessions; use `lh-harness plugin uninstall` to withdraw it.",
        )
        _notify(on_status, "installing", f"Installing {COMPUTER_USE_PLUGIN_ID}…")
        _install_plugin(binary, COMPUTER_USE_PLUGIN_ID)
        state = get_codex_plugin_state(COMPUTER_USE_PLUGIN_ID, codex_binary=binary)

    if state.installed and not state.enabled:
        # `plugin add` is idempotent and sets the enable bit itself, so re-run it
        # rather than hand-editing config.toml.
        _notify(on_status, "enabling", f"Enabling {COMPUTER_USE_PLUGIN_ID}…")
        _install_plugin(binary, COMPUTER_USE_PLUGIN_ID)
        state = get_codex_plugin_state(COMPUTER_USE_PLUGIN_ID, codex_binary=binary)

    if not state.ready:
        raise CodexPluginError(
            f"{COMPUTER_USE_PLUGIN_ID} is still not enabled after setup. "
            "A project or managed Codex policy may be overriding the user configuration."
        )
    if activate:
        _activate_runtime(on_status=on_status)
    _record()
    return state


def runtime_app_path() -> Path:
    """The macOS runtime app that actually holds the Accessibility grant."""
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    return codex_home / "computer-use" / "Codex Computer Use.app"


def codex_gui_grants() -> str:
    """Describe the grant situation; the plugin exposes no way to query it."""
    if sys.platform == "darwin":
        app = runtime_app_path()
        if not app.is_dir():
            return "runtime app not downloaded yet; run one Computer Use turn in Codex first"
        return (
            f"grant \u201c{app.stem}\u201d Accessibility + Screen Recording by hand "
            "(the plugin never prompts, and offers no way to query the state)"
        )
    if sys.platform == "win32":
        return (
            "no per-app grant exists; UI Automation only needs an interactive desktop session "
            "and an unelevated harness"
        )
    return "handled by Codex on this platform"


def _activate_runtime(*, on_status: StatusCallback | None) -> None:
    """Do whatever the current OS needs before GUI control can work."""
    if sys.platform == "darwin":
        _activate_macos_runtime(on_status=on_status)
    elif sys.platform == "win32":
        _activate_windows_runtime(on_status=on_status)


def _activate_macos_runtime(*, on_status: StatusCallback | None) -> None:
    """Get the runtime app listed under Accessibility, then point the user at it.

    The plugin ships no consent command, and its runtime queries permissions with
    the non-prompting `AXIsProcessTrusted`, so nothing ever raises a dialog on its
    own. Launching the app once is what makes macOS list it, after which the grant
    has to be toggled by hand.
    """
    app = runtime_app_path()
    if not app.is_dir():
        _notify(
            on_status,
            "todo",
            f"The runtime app is not downloaded yet ({app}). Run one Computer Use turn in "
            "Codex or the ChatGPT app to fetch it, then re-run this install to finish setup.",
        )
        return
    # `open -g` registers the bundle without stealing focus; it is LSUIElement anyway.
    try:
        result = subprocess.run(
            ["open", "-g", "-a", str(app)], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _notify(on_status, "warn", f"Could not launch {app.name}: {exc}")
    else:
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            _notify(on_status, "warn", f"Could not launch {app.name}: {detail[:200]}")
        else:
            _notify(on_status, "ok", f"Registered {app.name} with macOS")
    _notify(
        on_status,
        "todo",
        "Grant it Accessibility and Screen Recording yourself: this plugin never raises the "
        "permission dialog, so opening the pane is the only way. Enable "
        f"\u201c{app.stem}\u201d in System Settings > Privacy & Security > Accessibility, "
        "then again under Screen & System Audio Recording.",
    )
    for pane in ("Privacy_ScreenCapture", "Privacy_Accessibility"):
        subprocess.run(
            ["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"],
            capture_output=True,
            timeout=30,
            check=False,
        )


def _activate_windows_runtime(*, on_status: StatusCallback | None) -> None:
    """Report what Windows needs; there is no per-app grant to pre-authorize.

    UI Automation is gated by desktop session and integrity level rather than by
    a TCC-style permission, so nothing can be toggled ahead of time. What does
    fail silently is an elevated shell, or one with no desktop session at all.
    """
    _notify(
        on_status,
        "note",
        "Windows has no per-app GUI permission to grant: UI Automation only needs to run in "
        "your interactive desktop session. Keep this shell unelevated -- an elevated harness "
        "cannot drive normal apps, and elevated apps cannot be driven from an unelevated one.",
    )
    if not os.environ.get("SESSIONNAME"):
        _notify(
            on_status,
            "warn",
            "No interactive desktop session was detected (SESSIONNAME is unset), which is "
            "typical of a service or SSH shell. GUI control will fail until the harness runs "
            "inside a signed-in desktop session.",
        )


def _record() -> None:
    """Codex loads this plugin itself, so no MCP config is generated for it."""
    record_install(
        CODEX_GUI_PLUGIN_ID,
        agents=["codex"],
        mcp_configs={},
        mcp_server_name="",
    )


def uninstall_computer_use_plugin(
    *,
    on_status: StatusCallback | None = None,
    codex_binary: str | None = None,
) -> CodexPluginState:
    """Remove Codex Computer Use after explicit doctor opt-in."""

    binary = _resolve_codex_binary(codex_binary)
    state = get_codex_plugin_state(COMPUTER_USE_PLUGIN_ID, codex_binary=binary)
    if not state.installed:
        forget_install(CODEX_GUI_PLUGIN_ID)
        return state
    _notify(on_status, "removing", f"Removing {COMPUTER_USE_PLUGIN_ID}…")
    _remove_plugin(binary, COMPUTER_USE_PLUGIN_ID)
    state = get_codex_plugin_state(COMPUTER_USE_PLUGIN_ID, codex_binary=binary)
    if state.installed:
        raise CodexPluginError(
            f"{COMPUTER_USE_PLUGIN_ID} is still installed after Codex reported a successful removal."
        )
    forget_install(CODEX_GUI_PLUGIN_ID)
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
    source = entry.get("source")
    return CodexPluginState(
        plugin_id=plugin_id,
        installed=bool(entry.get("installed")),
        enabled=bool(entry.get("enabled")),
        available=True,
        version=str(entry.get("version") or ""),
        source_path=str(source.get("path") or "") if isinstance(source, dict) else "",
    )


def _resolve_codex_binary(explicit: str | None) -> str:
    if explicit:
        return explicit
    # Presence on PATH is not enough: a Microsoft Store alias resolves but never runs.
    cli = probe_agent_cli("codex")
    if not cli.found:
        raise CodexPluginError(
            "Codex CLI was not found. Install Codex and make sure `codex` is available on PATH."
        )
    if not cli.usable:
        raise CodexPluginError(cli.problem)
    return cli.path


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


def _notify(callback: StatusCallback | None, status: str, message: str) -> None:
    if callback is not None:
        callback(status, message)
