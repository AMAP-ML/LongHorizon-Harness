"""adapters/tools/workspace_read.py: the read-only list+grep gptme tool for
`workspace`-kind probes (PLAN.md §A6/§B4).

Only the stdlib-only, gptme-free half is tested here (`tool` itself is
`None` outside a real gptme process, same guard `searxng_search.py` uses;
CLAUDE.md Part III: the core test suite stays gptme-free) — `list_dir`,
`grep`, and the path-confinement helper they both route through.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.adapters.tools.workspace_read import (  # noqa: E402
    WorkspaceReadError,
    grep,
    list_dir,
)


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ListDirTest(unittest.TestCase):
    def test_lists_files_and_dirs_with_trailing_slash_on_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            _write(root / "a.py", "print(1)\n")
            (root / "pkg").mkdir()
            entries = list_dir(root, ".")
            self.assertIn("a.py", entries)
            self.assertIn("pkg/", entries)

    def test_missing_path_raises(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            with self.assertRaises(WorkspaceReadError):
                list_dir(Path(root_str), "does-not-exist")

    def test_cannot_escape_the_confined_root(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str) / "confined"
            root.mkdir()
            (Path(root_str) / "secret.txt").write_text("outside\n", encoding="utf-8")
            with self.assertRaises(WorkspaceReadError):
                list_dir(root, "../")

    def test_deep_traversal_also_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str) / "confined"
            root.mkdir()
            with self.assertRaises(WorkspaceReadError):
                list_dir(root, "a/b/../../../../etc")


class GrepTest(unittest.TestCase):
    def test_finds_matches_across_files_with_line_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            _write(root / "a.py", "def handler():\n    return TODO_fix_me\n")
            _write(root / "pkg" / "b.py", "# nothing here\n")
            matches = grep(root, "TODO_fix_me")
            self.assertEqual(len(matches), 1)
            self.assertTrue(matches[0].startswith("a.py:2:"))

    def test_grep_confined_to_a_subpath(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            _write(root / "a.py", "MARKER\n")
            _write(root / "pkg" / "b.py", "MARKER\n")
            matches = grep(root, "MARKER", "pkg")
            self.assertEqual(len(matches), 1)
            self.assertTrue(matches[0].startswith("pkg/b.py:"))

    def test_grep_caps_total_matches(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            for i in range(80):
                _write(root / f"f{i}.py", "MARKER\n")
            matches = grep(root, "MARKER")
            from kusudaemon.adapters.tools.workspace_read import MAX_GREP_MATCHES

            self.assertEqual(len(matches), MAX_GREP_MATCHES)

    def test_invalid_pattern_raises_workspace_read_error(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            with self.assertRaises(WorkspaceReadError):
                grep(Path(root_str), "(unterminated[")

    def test_grep_cannot_escape_the_confined_root(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str) / "confined"
            root.mkdir()
            with self.assertRaises(WorkspaceReadError):
                grep(root, "anything", "../")


if __name__ == "__main__":
    unittest.main()
