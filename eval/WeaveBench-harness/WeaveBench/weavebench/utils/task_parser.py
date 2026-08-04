"""Parse a WeaveBench task ``.md`` into the dict consumed by the runners.

A task file is YAML frontmatter (between ``---`` fences) followed by a Markdown
body whose ``## <Header>`` sections carry the task payload (Prompt, Workspace
Path, Skills, Env, Warmup, Automated Checks). Most section bodies are wrapped in
a single fenced code block, which :func:`strip_codeblock` unwraps.

Note: section splitting keys off any line starting with ``## ``; it does not
track fenced code blocks, so a literal ``## ...`` line *inside* a code block
would start a new section. Task authors should avoid top-level ``## `` lines
within section bodies.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

ROOT_DIR  = Path(__file__).resolve().parent.parent


def parse_task_md(task_file: Path) -> dict:
    """Extract task_id, prompt, workspace_path, and automated_checks from task.md."""
    content = task_file.read_text(encoding="utf-8")

    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
    if not fm_match:
        raise ValueError(f"YAML frontmatter not found: {task_file}")

    metadata = yaml.safe_load(fm_match.group(1))
    body     = fm_match.group(2)

    sections: dict[str, str] = {}
    current_section: Optional[str] = None
    lines: list[str] = []
    # Walk the body line by line: every "## <Header>" opens a new section and
    # flushes the accumulated lines of the previous one. (Fence-agnostic — see
    # the module docstring caveat about "## " lines inside code blocks.)
    for line in body.split("\n"):
        header = re.match(r"^##\s+(.+)$", line)
        if header:
            if current_section is not None:
                sections[current_section] = "\n".join(lines).strip()
            current_section = header.group(1)
            lines = []
        else:
            lines.append(line)
    # Flush the final section (the loop only writes a section when the *next*
    # header is seen, so the last one would otherwise be dropped).
    if current_section is not None:
        sections[current_section] = "\n".join(lines).strip()

    def strip_codeblock(raw: str) -> str:
        # Unwrap a single leading ```lang fence and a single trailing ``` fence.
        # Only the outermost pair is removed; nested fences are left intact.
        s = re.sub(r"^```[^\n]*\n?", "", raw.strip())
        s = re.sub(r"\n?```$", "", s).strip()
        return s

    prompt = sections.get("Prompt", "").strip()

    raw_workspace  = sections.get("Workspace Path", "").strip()
    workspace_path = strip_codeblock(raw_workspace)
    if not workspace_path:
        raise ValueError(f"Missing ## Workspace Path in task.md: {task_file}")

    skills_path = "skills"

    automated_checks = strip_codeblock(sections.get("Automated Checks", ""))
    env    = strip_codeblock(sections.get("Env",    ""))
    skills = strip_codeblock(sections.get("Skills",    ""))
    warmup = strip_codeblock(sections.get("Warmup", ""))

    task_id         = metadata.get("id",             task_file.stem)
    # Defaults mirror the runners' expectations: no step cap, and a 24h wall
    # clock (86400s) so long-horizon tasks aren't truncated unless a task opts
    # into a tighter bound. ``gui`` defaults to headless/CLI mode.
    timeout_seconds = int(metadata.get("timeout_seconds", 86400))
    gui             = bool(metadata.get("gui", False))
    max_steps       = metadata.get("max_steps", None)
    if max_steps is not None:
        max_steps = int(max_steps)

    # Relative workspace/skills paths are resolved against the package root so a
    # task .md works regardless of the caller's current working directory.
    wp = Path(workspace_path)
    if not wp.is_absolute():
        wp = (ROOT_DIR / wp).resolve()
    workspace_path = str(wp)

    sp = Path(skills_path)
    if not sp.is_absolute():
        sp = (ROOT_DIR / sp).resolve()
    skills_path = str(sp)

    return {
        "task_id":          task_id,
        "prompt":           prompt,
        "workspace_path":   workspace_path,
        "skills_path":      skills_path,
        "automated_checks": automated_checks,
        "env":              env,
        "skills":           skills,
        "warmup":           warmup,
        "timeout_seconds":  timeout_seconds,
        "gui":              gui,
        "max_steps":        max_steps,
        "file_path":        str(task_file.resolve()),
        "category":         task_file.parent.name,
    }