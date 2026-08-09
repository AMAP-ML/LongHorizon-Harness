"""Compile gate tests (PLAN.md §4.6.3). Runs through the real
LocalEnvironment against plain shell commands -- no LaTeX toolchain needed,
since the gate is a generic injected command (see compile.py's docstring).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir  # noqa: E402
from kusudaemon.v3.compile import run_compile  # noqa: E402
from kusudaemon.v3.run_dir import assembly_dir, compile_log_path  # noqa: E402


class CompileGateTest(unittest.TestCase):
    def test_no_command_configured_is_a_trivial_pass(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            result = asyncio.run(run_compile(run_dir, LocalEnvironment(), None))
            self.assertTrue(result.passed)
            self.assertTrue(result.skipped)
            self.assertTrue(compile_log_path(run_dir).exists())

    def test_successful_command_passes_and_writes_log(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            result = asyncio.run(
                run_compile(run_dir, LocalEnvironment(), "echo build-ok")
            )
            self.assertTrue(result.passed)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("build-ok", result.log)
            self.assertIn("build-ok", compile_log_path(run_dir).read_text(encoding="utf-8"))

    def test_failing_command_fails_and_captures_exit_code_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            result = asyncio.run(
                run_compile(
                    run_dir, LocalEnvironment(),
                    "echo something-broke-in-ch03.md >&2; exit 1",
                )
            )
            self.assertFalse(result.passed)
            self.assertEqual(result.exit_code, 1)
            self.assertIn("something-broke-in-ch03.md", result.log)

    def test_runs_inside_assembly_dir_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            (assembly_dir(run_dir) / "main.md").write_text("hello", encoding="utf-8")
            result = asyncio.run(run_compile(run_dir, LocalEnvironment(), "cat main.md"))
            self.assertTrue(result.passed)
            self.assertIn("hello", result.log)


if __name__ == "__main__":
    unittest.main()
