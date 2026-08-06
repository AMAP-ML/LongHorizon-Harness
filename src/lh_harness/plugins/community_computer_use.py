"""Community computer-use MCP servers that the harness can set up for an agent.

Each entry is npm-distributed. The harness installs the global package and
grants its OS permissions, but writes the MCP wiring itself into
`~/.lh-harness/plugins/` -- never into `~/.codex/config.toml` or Claude's user
config. The vendors' own `install-*-mcp` helpers are deliberately not used
because they register the server globally, which would hand GUI control to every
unrelated Codex/Claude session. Nothing here runs during `lh-harness run`;
setup is always an explicit `lh-harness plugin` opt-in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .errors import PluginError
from .npm import (
    NpmPackageState,
    global_package_state,
    install_global_package,
    require_npm,
    uninstall_global_package,
)
from .state import forget_install, plugin_dir, record_install, write_mcp_config

StatusCallback = Callable[[str, str], None]

AGENT_CODEX = "codex"
AGENT_CLAUDE_CODE = "claude_code"


@dataclass(frozen=True)
class CommunityPlugin:
    plugin_id: str
    package: str
    summary: str
    homepage: str
    # Agents this server can drive. The harness writes each one's MCP config
    # itself, so no vendor registration command is involved.
    agents: tuple[str, ...]
    # Commands the user must run themselves (consent, OS permission grants).
    manual_steps: tuple[str, ...] = ()
    cli_name: str = ""
    mcp_server_name: str = ""
    # Args the MCP host uses to spawn the stdio server.
    mcp_args: tuple[str, ...] = ()
    # `sys.platform` values the plugin runs on.
    platforms: tuple[str, ...] = ("darwin", "linux", "win32")
    # Per-platform activation argv run during install; `{binary}` is resolved.
    activation: dict[str, tuple[tuple[str, ...], ...]] = field(default_factory=dict)
    # Per-platform readiness check run after activation to confirm the grants
    # took. Platforms that need no grants are intentionally left out.
    status_check: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Per-platform prerequisites the harness cannot install itself.
    prerequisites: dict[str, tuple[str, ...]] = field(default_factory=dict)
    extra_notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def command_name(self) -> str:
        return self.cli_name or self.package

    def supports(self, agent: str) -> bool:
        return agent in self.agents

    def supports_platform(self, platform_name: str) -> bool:
        return platform_name in self.platforms


OPEN_COMPUTER_USE = CommunityPlugin(
    plugin_id="open-computer-use",
    package="open-computer-use",
    cli_name="open-computer-use",
    mcp_server_name="open-computer-use",
    summary="Open-source Codex Computer Use alternative (macOS Swift, Windows UIA, Linux AT-SPI).",
    homepage="https://github.com/iFurySt/open-codex-computer-use",
    agents=(AGENT_CODEX, AGENT_CLAUDE_CODE),
    mcp_args=("mcp",),
    # Only the macOS runtime needs grants; the Windows/Linux `doctor` just
    # prints a session note and always exits 0, so checking it proves nothing.
    activation={"darwin": (("{binary}", "doctor"),)},
    status_check={"darwin": ("{binary}", "doctor")},
    prerequisites={
        "darwin": ("macOS 14.0 or later.",),
        "linux": ("Run inside the signed-in desktop session (AT-SPI2 needs it).",),
        "win32": ("Run inside the signed-in desktop session (UI Automation needs it).",),
    },
)

CLAWDCURSOR = CommunityPlugin(
    plugin_id="clawdcursor",
    package="clawdcursor",
    cli_name="clawdcursor",
    mcp_server_name="clawdcursor",
    summary="Local MCP server that compiles the screen into a verified UI map and acts on element ids.",
    homepage="https://github.com/AmrDab/clawdcursor",
    agents=(AGENT_CODEX, AGENT_CLAUDE_CODE),
    mcp_args=("mcp", "--compact"),
    activation={
        # Consent is required on every OS; `grant` walks the macOS TCC dialogs.
        "darwin": (
            ("{binary}", "consent", "--accept"),
            ("{binary}", "grant"),
        ),
        "linux": (("{binary}", "consent", "--accept"),),
        "win32": (("{binary}", "consent", "--accept"),),
    },
    status_check={
        "darwin": ("{binary}", "status"),
        "linux": ("{binary}", "status"),
        "win32": ("{binary}", "status"),
    },
    prerequisites={
        "darwin": ("Xcode Command Line Tools (`xcode-select --install`) for screenshots/vision.",),
        "linux": (
            "apt install tesseract-ocr python3-gi gir1.2-atspi-2.0",
            "On Wayland also install ydotool (with ydotoold) or wtype.",
        ),
    },
    extra_notes=("Requires Node.js 20 or later.",),
)

COMMUNITY_PLUGINS: tuple[CommunityPlugin, ...] = (OPEN_COMPUTER_USE, CLAWDCURSOR)
_BY_ID = {plugin.plugin_id: plugin for plugin in COMMUNITY_PLUGINS}


def community_plugin_ids() -> tuple[str, ...]:
    return tuple(plugin.plugin_id for plugin in COMMUNITY_PLUGINS)


def get_community_plugin(plugin_id: str) -> CommunityPlugin:
    plugin = _BY_ID.get(plugin_id)
    if plugin is None:
        raise PluginError(
            f"Unknown plugin {plugin_id!r}. Available: {', '.join(community_plugin_ids())}."
        )
    return plugin


def community_plugin_state(plugin: CommunityPlugin) -> NpmPackageState:
    return global_package_state(plugin.package)


def community_plugin_activation(plugin: CommunityPlugin) -> tuple[bool | None, str]:
    """Ask the vendor whether its consent and OS permissions are in place.

    Returns `(ready, detail)`; `ready` is None when this platform needs no grants.
    """
    template = plugin.status_check.get(sys.platform)
    if not template:
        return None, f"no grants are needed on {sys.platform}"
    try:
        argv = _resolve_argv(plugin, template)
    except PluginError as exc:
        return False, str(exc)
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run `{' '.join(argv)}`: {exc}"
    detail = ((result.stdout or "") + (result.stderr or "")).strip()
    summary = detail.splitlines()[-1][:160] if detail else f"exit {result.returncode}"
    return result.returncode == 0, summary


def install_community_plugin(
    plugin: CommunityPlugin,
    *,
    agents: Sequence[str],
    on_status: StatusCallback | None = None,
    activate: bool = True,
) -> NpmPackageState:
    """Install the npm package, activate it, then wire it to each agent."""
    npm = require_npm()
    unsupported = [agent for agent in agents if not plugin.supports(agent)]
    if unsupported:
        raise PluginError(
            f"{plugin.plugin_id} does not support: {', '.join(unsupported)}. "
            f"Supported: {', '.join(sorted(plugin.agents))}."
        )
    if not plugin.supports_platform(sys.platform):
        raise PluginError(
            f"{plugin.plugin_id} does not support {sys.platform}. "
            f"Supported platforms: {', '.join(sorted(plugin.platforms))}."
        )

    for line in plugin.prerequisites.get(sys.platform, ()):
        _notify(on_status, "note", f"Prerequisite: {line}")

    state = global_package_state(plugin.package, npm=npm)
    if state.installed:
        _notify(on_status, "ok", f"{plugin.package} {state.version} is already installed")
    else:
        _notify(on_status, "installing", f"Installing {plugin.package} globally via npm…")
        state = install_global_package(plugin.package, npm=npm)
        if not state.installed:
            raise PluginError(f"npm reported success but {plugin.package} is still not installed.")
        _notify(on_status, "ok", f"Installed {plugin.package} {state.version}")

    if activate:
        _activate(plugin, on_status=on_status)

    mcp_configs: dict[str, str] = {}
    for agent in agents:
        config = write_mcp_config(
            plugin.plugin_id,
            agent,
            server_name=plugin.mcp_server_name,
            command=plugin.command_name,
            args=list(plugin.mcp_args),
        )
        mcp_configs[agent] = str(config)
        _notify(on_status, "ok", f"Wrote {agent} MCP config: {config}")

    record_install(
        plugin.plugin_id,
        agents=list(agents),
        mcp_configs=mcp_configs,
        mcp_server_name=plugin.mcp_server_name,
    )
    _warn_about_global_registration(plugin, on_status=on_status)
    for step in plugin.manual_steps:
        _notify(on_status, "todo", f"Run manually: {step}")
    for note in plugin.extra_notes:
        _notify(on_status, "note", note)
    return state


def _activate(plugin: CommunityPlugin, *, on_status: StatusCallback | None) -> None:
    """Run the vendor's consent / OS-permission commands, then verify them."""
    for template in plugin.activation.get(sys.platform, ()):
        argv = _resolve_argv(plugin, template)
        _notify(on_status, "activating", " ".join(argv))
        try:
            result = subprocess.run(argv, timeout=600, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PluginError(f"Could not activate {plugin.plugin_id}: {exc}") from exc
        if result.returncode != 0:
            # Permission dialogs are user-driven and may be declined or deferred,
            # so a non-zero exit must not undo an otherwise good install.
            _notify(
                on_status,
                "warn",
                f"`{' '.join(argv)}` exited {result.returncode}; "
                "grant the OS permissions manually and re-run it if GUI control fails",
            )
    _verify_activation(plugin, on_status=on_status)


def _verify_activation(plugin: CommunityPlugin, *, on_status: StatusCallback | None) -> None:
    """Confirm the grants actually took, instead of trusting the exit code."""
    ready, detail = community_plugin_activation(plugin)
    if ready is None:
        return
    if ready:
        _notify(on_status, "ok", f"Activation verified: {detail}")
        return
    _notify(
        on_status,
        "warn",
        f"The plugin reports it is not ready ({detail}). "
        "GUI control will fail until consent and the OS permissions are granted.",
    )


def uninstall_community_plugin(
    plugin: CommunityPlugin,
    *,
    on_status: StatusCallback | None = None,
) -> NpmPackageState:
    npm = require_npm()
    state = global_package_state(plugin.package, npm=npm)
    if not state.installed:
        _notify(on_status, "ok", f"{plugin.package} is not installed")
    else:
        _notify(on_status, "removing", f"Removing {plugin.package}…")
        state = uninstall_global_package(plugin.package, npm=npm)
        if state.installed:
            raise PluginError(f"{plugin.package} is still installed after npm reported success.")
        _notify(on_status, "ok", f"Removed {plugin.package}")

    forget_install(plugin.plugin_id)
    generated = plugin_dir(plugin.plugin_id)
    if generated.is_dir():
        shutil.rmtree(generated, ignore_errors=True)
        _notify(on_status, "ok", f"Removed generated MCP configs under {generated}")
    _warn_about_global_registration(plugin, on_status=on_status)
    return state


def global_registrations(plugin: CommunityPlugin) -> list[str]:
    """Find leftovers in the agents' own configs, e.g. from a vendor installer.

    The harness never writes there, but earlier versions did and the vendors'
    `install-*-mcp` helpers still do, so report them instead of editing them.
    """
    found: list[str] = []
    name = plugin.mcp_server_name
    for path, needle in _global_config_probes(name):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if needle in text:
            found.append(str(path))
    return found


def _global_config_probes(name: str) -> tuple[tuple[Path, str], ...]:
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    return (
        (codex_home / "config.toml", f"[mcp_servers.{name}]"),
        (codex_home / "config.toml", f'[mcp_servers."{name}"]'),
        (Path.home() / ".claude.json", f'"{name}"'),
    )


def _warn_about_global_registration(
    plugin: CommunityPlugin, *, on_status: StatusCallback | None
) -> None:
    leftovers = global_registrations(plugin)
    if not leftovers:
        return
    _notify(
        on_status,
        "todo",
        f"{plugin.mcp_server_name} is still registered globally in "
        f"{', '.join(sorted(set(leftovers)))}; the harness never writes there, so remove it "
        f"yourself (`claude mcp remove {plugin.mcp_server_name} -s user`, or delete the "
        f"[mcp_servers.{plugin.mcp_server_name}] block) to keep GUI control out of unrelated sessions",
    )


def _resolve_argv(plugin: CommunityPlugin, template: tuple[str, ...]) -> list[str]:
    argv: list[str] = []
    for index, item in enumerate(template):
        if item != "{binary}":
            argv.append(item)
            continue
        resolved = shutil.which(plugin.command_name)
        if not resolved:
            raise PluginError(
                f"`{plugin.command_name}` was not found on PATH after installing "
                f"{plugin.package}. Ensure npm's global bin directory is on PATH."
            )
        # A leading {binary} is the executable; later ones are arguments.
        argv.append(resolved if index == 0 else plugin.command_name)
    return argv


def _notify(callback: StatusCallback | None, status: str, message: str) -> None:
    if callback is not None:
        callback(status, message)
