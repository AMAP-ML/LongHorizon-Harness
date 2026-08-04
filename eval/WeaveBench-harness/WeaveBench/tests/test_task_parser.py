"""Parse a synthetic task .md and confirm every documented field surfaces."""
from __future__ import annotations

import pytest

from weavebench.utils.task_parser import parse_task_md


MINIMAL_TASK_MD = """---
id: WEB_task_42_synthetic
gui: true
timeout_seconds: 600
max_steps: 50
---

## Prompt

Pretend to do a real task so we can verify parser output.

## Workspace Path

```
/tmp/weavebench_test_workspace
```

## Skills

```
# (no skills)
```

## Env

```
export FOO=bar
```

## Warmup

```bash
echo "warmup"
```

## Automated Checks

```python
def grade(workspace_path, transcript):
    return {"score": 1.0, "details": {}}
```
"""


def test_parse_minimal(tmp_path):
    cat_dir = tmp_path / "WEB"
    cat_dir.mkdir()
    md = cat_dir / "WEB_task_42_synthetic.md"
    md.write_text(MINIMAL_TASK_MD, encoding="utf-8")

    task = parse_task_md(md)

    assert task["task_id"] == "WEB_task_42_synthetic"
    assert task["category"] == "WEB"
    assert task["prompt"].startswith("Pretend to do a real task")
    assert task["workspace_path"] == "/tmp/weavebench_test_workspace"
    assert task["gui"] is True
    assert task["timeout_seconds"] == 600
    assert task["max_steps"] == 50
    assert "FOO=bar" in task["env"]
    assert "warmup" in task["warmup"]
    assert "def grade" in task["automated_checks"]
    assert task["file_path"].endswith("WEB_task_42_synthetic.md")


def test_parse_missing_frontmatter_raises(tmp_path):
    md = tmp_path / "broken.md"
    md.write_text("# just a regular markdown doc\n", encoding="utf-8")
    with pytest.raises(ValueError, match="YAML frontmatter"):
        parse_task_md(md)


def test_parse_missing_workspace_raises(tmp_path):
    md = tmp_path / "no_workspace.md"
    md.write_text("---\nid: x\n---\n## Prompt\n\nhi\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Workspace Path"):
        parse_task_md(md)
