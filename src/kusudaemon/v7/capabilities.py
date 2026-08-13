"""Agent Skills, Plugins, MCP, and Capability Discovery (§K).

Discovers skills, plugins, and MCP server configurations from global
(~/.gemini/config) and workspace (.agents) roots, parsing SKILL.md
instructions and mcp_config.json configurations for agent context injection.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    instructions: str


@dataclass
class MCPServer:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def _parse_skill_file(skill_md_path: Path) -> Skill | None:
    if not skill_md_path.is_file():
        return None
    try:
        content = skill_md_path.read_text(encoding="utf-8")
    except OSError:
        return None

    name = skill_md_path.parent.name
    description = ""
    instructions = content

    # Parse YAML frontmatter if present
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1]
            instructions = parts[2].strip()
            for line in frontmatter.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    k = k.strip()
                    v = v.strip().strip("\"'")
                    if k == "name" and v:
                        name = v
                    elif k == "description" and v:
                        description = v

    return Skill(
        name=name,
        description=description,
        path=skill_md_path.parent,
        instructions=instructions,
    )


def discover_skills(workspace_root: Path | str | None = None) -> list[Skill]:
    """Discover available skills from global and workspace roots."""
    skills: dict[str, Skill] = {}
    roots: list[Path] = []

    # Global config root
    home = Path.home()
    global_root = home / ".gemini" / "config"
    if global_root.is_dir():
        roots.append(global_root)

    # Workspace root
    if workspace_root is not None:
        ws_path = Path(workspace_root)
        agents_root = ws_path / ".agents"
        if agents_root.is_dir():
            roots.append(agents_root)

    for root in roots:
        # Standalone skills directory
        skills_dir = root / "skills"
        if skills_dir.is_dir():
            for child in skills_dir.iterdir():
                if child.is_dir():
                    skill = _parse_skill_file(child / "SKILL.md")
                    if skill is not None:
                        skills[skill.name] = skill

        # Plugin bundles with skills
        plugins_dir = root / "plugins"
        if plugins_dir.is_dir():
            for plugin in plugins_dir.iterdir():
                if plugin.is_dir() and (plugin / "skills").is_dir():
                    for child in (plugin / "skills").iterdir():
                        if child.is_dir():
                            skill = _parse_skill_file(child / "SKILL.md")
                            if skill is not None:
                                skills[skill.name] = skill

    return list(skills.values())


def discover_mcp_servers(workspace_root: Path | str | None = None) -> dict[str, MCPServer]:
    """Discover MCP server configurations from global and workspace roots."""
    servers: dict[str, MCPServer] = {}
    config_paths: list[Path] = []

    home = Path.home()
    config_paths.append(home / ".gemini" / "config" / "mcp_config.json")

    if workspace_root is not None:
        ws_path = Path(workspace_root)
        config_paths.append(ws_path / ".agents" / "mcp_config.json")

    for cfg_path in config_paths:
        if not cfg_path.is_file():
            continue
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        raw_servers = data.get("mcpServers") or data.get("servers") or {}
        if isinstance(raw_servers, dict):
            for name, spec in raw_servers.items():
                if isinstance(spec, dict):
                    cmd = str(spec.get("command", ""))
                    args = [str(a) for a in (spec.get("args") or [])]
                    env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
                    if cmd:
                        servers[str(name)] = MCPServer(
                            name=str(name),
                            command=cmd,
                            args=args,
                            env=env,
                        )

    return servers


def build_capabilities_prompt(workspace_root: Path | str | None = None) -> str:
    """Format discovered skills and MCP tools into a system prompt section."""
    skills = discover_skills(workspace_root)
    mcp_servers = discover_mcp_servers(workspace_root)

    if not skills and not mcp_servers:
        return ""

    lines = ["## Available Capabilities and Extensions"]
    if skills:
        lines.append("\n### Skills:")
        for s in skills:
            desc = f": {s.description}" if s.description else ""
            lines.append(f"- **{s.name}** ({s.path}){desc}")

    if mcp_servers:
        lines.append("\n### MCP Servers:")
        for name, s in mcp_servers.items():
            lines.append(f"- **{name}**: `{s.command} {' '.join(s.args)}`".strip())

    return "\n".join(lines)
