"""discover_tasks: ensure a synthetic two-task tree is parsed without leaking
temp directories or losing per-category structure."""
from __future__ import annotations

import tempfile
from pathlib import Path

from weavebench.eval import orchestrator


TASK_TEMPLATE = """---
id: {tid}
gui: false
timeout_seconds: 60
---

## Prompt

A test task.

## Workspace Path

```
/tmp/weavebench_workspace_{tid}
```

## Automated Checks

```python
def grade(workspace_path, transcript):
    return {{"score": 0.0}}
```
"""


def _make_tree(root: Path) -> None:
    web = root / "WEB"
    dav = root / "DAV"
    web.mkdir(parents=True)
    dav.mkdir(parents=True)
    (web / "WEB_task_1_foo.md").write_text(TASK_TEMPLATE.format(tid="WEB_task_1_foo"),
                                            encoding="utf-8")
    (dav / "DAV_task_2_bar.md").write_text(TASK_TEMPLATE.format(tid="DAV_task_2_bar"),
                                            encoding="utf-8")


def test_discover_finds_both_categories(tmp_path):
    _make_tree(tmp_path)
    tasks = orchestrator.discover_tasks(tmp_path, ["WEB", "DAV"])
    ids = sorted(t["task_id"] for t in tasks)
    assert ids == ["DAV_task_2_bar", "WEB_task_1_foo"]
    cats = sorted({t["category"] for t in tasks})
    assert cats == ["DAV", "WEB"]


def test_discover_missing_category_logs_but_continues(tmp_path):
    _make_tree(tmp_path)
    tasks = orchestrator.discover_tasks(tmp_path, ["WEB", "NONEXISTENT"])
    assert len(tasks) == 1
    assert tasks[0]["category"] == "WEB"


def test_discover_does_not_leak_tempdir(tmp_path):
    """discover_tasks used to leak tempfile.mkdtemp; now uses TemporaryDirectory."""
    _make_tree(tmp_path)
    before = set(Path(tempfile.gettempdir()).glob("weavebench_md_*"))
    orchestrator.discover_tasks(tmp_path, ["WEB", "DAV"])
    after = set(Path(tempfile.gettempdir()).glob("weavebench_md_*"))
    assert before == after, f"leaked tempdirs: {after - before}"
