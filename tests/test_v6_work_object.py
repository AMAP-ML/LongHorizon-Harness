"""v6 work object tests (PLAN.md §A3, §B1). No network, no model — every
measurement here is pure filesystem + stdlib, per the module's own
invariant that est_tokens must never require a model call.
"""

from __future__ import annotations

import asyncio
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.adapters.cli_agent import CommandAgentAdapter  # noqa: E402
from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.pipeline.backends import build_writer_adapter  # noqa: E402
from kusudaemon.pipeline.prompts import build_node_prompt  # noqa: E402
from kusudaemon.types import EpisodeBudget  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir, node_artifact_path  # noqa: E402
from kusudaemon.v1.gates import estimate_tokens  # noqa: E402
from kusudaemon.v1.tree import TaskNode  # noqa: E402
from kusudaemon.v6.work_object import (  # noqa: E402
    DEFAULT_MAX_FILE_BYTES,
    WorkObject,
    measure_workspace,
    survey_workspace,
    work_object_from_text,
    work_object_none,
)

_FIXTURE_SCRIPT = _REPO_ROOT / "tests" / "fixtures" / "fake_workspace_writer.py"


class MeasureWorkspaceTest(unittest.TestCase):
    """§B1's own test list: "measurement excludes binaries/.git/
    node_modules"."""

    def _build_fixture(self, root: Path) -> None:
        (root / "src").mkdir()
        (root / "readme.md").write_text("hello world " * 20, encoding="utf-8")
        (root / "src" / "app.py").write_text("print('hi')\n" * 20, encoding="utf-8")

        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")

        (root / "node_modules" / "pkg").mkdir(parents=True)
        (root / "node_modules" / "pkg" / "index.js").write_text("module.exports = {}\n" * 50, encoding="utf-8")

        # Extension-denylisted binary.
        (root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        # Null-byte-sniffed binary with a non-denylisted extension.
        (root / "blob.dat").write_bytes(b"some header" + b"\x00" + b"more bytes after a null")

        (root / "package-lock.json").write_text("{}" * 500, encoding="utf-8")

    def test_excludes_binaries_git_node_modules_and_lockfiles(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            self._build_fixture(root)
            work = measure_workspace(root)

        self.assertEqual(work.kind, "workspace")
        self.assertEqual(work.files, 2)  # readme.md, src/app.py only
        self.assertGreater(work.bytes, 0)
        self.assertGreater(work.est_tokens, 0)
        # Nothing counted came from .git, node_modules, the binaries, or the
        # lockfile -- indirectly proven by the exact file count above, but
        # also check bytes stay small (a 100-line node_modules file or the
        # lockfile's ~1000 chars would visibly inflate this otherwise).
        self.assertLess(work.bytes, 2000)

    def test_top_dirs_groups_by_top_level_directory(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            self._build_fixture(root)
            work = measure_workspace(root)

        top_dir_names = {name for name, _tokens in work.top_dirs}
        self.assertIn("src", top_dir_names)
        self.assertIn(".", top_dir_names)  # readme.md, a root-level file
        # Descending by tokens.
        tokens = [tokens for _name, tokens in work.top_dirs]
        self.assertEqual(tokens, sorted(tokens, reverse=True))

    def test_size_ceiling_excludes_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            (root / "small.txt").write_text("small file content\n", encoding="utf-8")
            (root / "huge.txt").write_text("x" * (DEFAULT_MAX_FILE_BYTES + 1), encoding="utf-8")
            work = measure_workspace(root)

        self.assertEqual(work.files, 1)
        self.assertLess(work.bytes, DEFAULT_MAX_FILE_BYTES)

    def test_gitignore_is_respected(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            (root / ".gitignore").write_text("ignored/\n*.log\n", encoding="utf-8")
            (root / "ignored").mkdir()
            (root / "ignored" / "data.txt").write_text("should not be counted\n", encoding="utf-8")
            (root / "app.log").write_text("should not be counted either\n", encoding="utf-8")
            (root / "keep.txt").write_text("this one counts\n", encoding="utf-8")
            work = measure_workspace(root)
            # .gitignore's own text is legitimate workspace content and
            # counts too -- the members list (not a bare total, which would
            # make that ambiguous) is what actually proves the ignored
            # patterns were excluded.
            members = {m for unit in survey_workspace(work) for m in unit.members}

        self.assertIn("keep.txt", members)
        self.assertNotIn("app.log", members)
        self.assertNotIn("ignored/data.txt", members)
        self.assertGreater(work.est_tokens, 0)

    def test_empty_directory_measures_to_zero(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            work = measure_workspace(root_str)
        self.assertEqual(work.files, 0)
        self.assertEqual(work.bytes, 0)
        self.assertEqual(work.est_tokens, 0)
        self.assertEqual(work.top_dirs, ())


class WorkObjectFromTextTest(unittest.TestCase):
    """§B1: "kind='text' construction from a legacy source_text spec is
    byte-identical to today." The corpus flows through the pipeline as a
    plain string (RunOptions.source_text -> source.txt, unchanged by this
    module); this constructor just measures that same string the same way
    every other token count in this repo is measured
    (v1/gates.estimate_tokens), without touching disk."""

    def test_measures_the_in_memory_string_with_the_shared_heuristic(self) -> None:
        text = "one two three four five " * 10
        work = work_object_from_text(text)

        self.assertEqual(work.kind, "text")
        self.assertIsNone(work.root)
        self.assertIsNone(work.text_path)
        self.assertEqual(work.files, 1)
        self.assertEqual(work.bytes, len(text.encode("utf-8")))
        self.assertEqual(work.est_tokens, estimate_tokens(text))
        self.assertEqual(work.top_dirs, ())

    def test_empty_source_text_yields_zero_files(self) -> None:
        work = work_object_from_text("")
        self.assertEqual(work.files, 0)
        self.assertEqual(work.bytes, 0)
        self.assertEqual(work.est_tokens, 0)

    def test_whitespace_only_source_text_yields_zero_files(self) -> None:
        # Matches _phase_survey's own "no source" check (source.strip()),
        # not a naive truthiness check on the raw string.
        work = work_object_from_text("   \n\n  ")
        self.assertEqual(work.files, 0)

    def test_optional_text_path_is_recorded_as_provided(self) -> None:
        work = work_object_from_text("some text", text_path="/tmp/corpus.txt")
        self.assertEqual(work.text_path, Path("/tmp/corpus.txt"))


class WorkObjectNoneTest(unittest.TestCase):
    def test_all_zero_and_constructible(self) -> None:
        work = work_object_none()
        self.assertEqual(work.kind, "none")
        self.assertIsNone(work.root)
        self.assertIsNone(work.text_path)
        self.assertEqual(work.files, 0)
        self.assertEqual(work.bytes, 0)
        self.assertEqual(work.est_tokens, 0)
        self.assertEqual(work.top_dirs, ())
        # Directly constructible too, per PLAN.md §B1's own phrasing.
        direct = WorkObject(
            kind="none", root=None, text_path=None, include=(), exclude=(),
            files=0, bytes=0, est_tokens=0, top_dirs=(),
        )
        self.assertEqual(direct, work)


class SurveyWorkspaceTest(unittest.TestCase):
    """§B1: "a workspace unit's members resolve to real files"."""

    def test_members_resolve_to_real_files_under_the_fixture_root(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            (root / "src").mkdir()
            (root / "src" / "a.py").write_text("a = 1\n" * 10, encoding="utf-8")
            (root / "src" / "b.py").write_text("b = 2\n" * 10, encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "readme.md").write_text("docs " * 10, encoding="utf-8")
            (root / "top.txt").write_text("top level " * 10, encoding="utf-8")

            work = measure_workspace(root)
            units = survey_workspace(work)

            # Assertions stay inside the tempdir's lifetime -- resolving
            # `member` against `root` only means something while the
            # fixture still exists on disk.
            self.assertGreater(len(units), 0)
            all_members: list[str] = []
            for unit in units:
                self.assertEqual(unit.start_chunk, -1)
                self.assertEqual(unit.end_chunk, -1)
                self.assertGreater(len(unit.members), 0)
                for member in unit.members:
                    self.assertFalse(Path(member).is_absolute())
                    resolved = root / member
                    self.assertTrue(resolved.is_file(), f"{resolved} should exist")
                    all_members.append(member)
        # Every measured file shows up in exactly one unit.
        self.assertEqual(len(all_members), len(set(all_members)))
        self.assertEqual(len(all_members), work.files)

    def test_oversized_group_is_split_across_multiple_units(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            (root / "src").mkdir()
            # Each file is a few hundred tokens; a low ceiling forces a
            # split of the "src" group into more than one unit.
            for i in range(5):
                (root / "src" / f"f{i}.py").write_text("word " * 200, encoding="utf-8")
            work = measure_workspace(root)
            units = survey_workspace(work, max_unit_tokens=300)

        src_units = [u for u in units if u.members and u.members[0].startswith("src/")]
        self.assertGreater(len(src_units), 1)
        for unit in src_units:
            self.assertLessEqual(unit.tokens, 300 + 300)  # one file's worth of slack

    def test_raises_for_non_workspace_kind(self) -> None:
        with self.assertRaises(ValueError):
            survey_workspace(work_object_none())
        with self.assertRaises(ValueError):
            survey_workspace(work_object_from_text("hello"))


class RunDirHiddenFromWorkspaceTest(unittest.TestCase):
    """§B1: "the run dir is never a subdirectory the Writer is told to
    edit." The default --workspace runs_root nests the run directory
    inside the repo the Writer is patching; build_writer_adapter must hide
    that whole subtree (not just the corpus-mode filenames, which don't
    even exist relative to the workspace's real cwd) while still carving
    out the node's own artifact/scratch paths."""

    def test_nested_run_dir_is_hidden_with_its_own_node_carved_out(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = root / ".kusudaemon" / "runs" / "r1"
            run_dir.mkdir(parents=True)
            node = TaskNode(id="n1", brief="b", artifact="out/n1.md", gates=["nonempty"])

            import os

            env_keys = (
                "KUSUDAEMON_PROVIDER_BASE_URL", "KUSUDAEMON_PROVIDER_API_KEY",
                "KUSUDAEMON_PROVIDER_MODEL", "KUSUDAEMON_PROVIDER_CONFIG",
                "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "KUSUDAEMON_PROVIDER",
            )
            env_backup = {key: os.environ.pop(key, None) for key in env_keys}
            os.environ["KUSUDAEMON_PROVIDER_CONFIG"] = "/nonexistent/provider.json"
            os.environ["OPENAI_API_KEY"] = "test-key"
            os.environ["OPENAI_BASE_URL"] = "https://test.example.com/v1"
            os.environ["OPENAI_MODEL"] = "test-model"
            try:
                adapter = build_writer_adapter(
                    "gptme",
                    workspace_path=root,
                    prompt_dir=run_dir / "tmp" / "prompts",
                    node=node,
                    run_dir=run_dir,
                )
            finally:
                for key, value in env_backup.items():
                    if value is not None:
                        os.environ[key] = value
                    else:
                        os.environ.pop(key, None)

        # The run dir subtree is hidden as a whole, relative to the
        # Writer's actual cwd (root) -- never left implicitly visible.
        rel_run_dir = ".kusudaemon/runs/r1/"
        self.assertIn(rel_run_dir, adapter.hidden_paths)
        # But this node's own two paths, expressed the same way, are
        # explicitly carved back out.
        self.assertIn(".kusudaemon/runs/r1/out/n1.md", adapter.hidden_path_exceptions)
        self.assertIn(".kusudaemon/runs/r1/scratch/n1", adapter.hidden_path_exceptions)


class WorkspaceShipGateTest(unittest.TestCase):
    """PLAN.md §B1's ship gate: "a gptme Writer dispatched with
    kind='workspace' can read and patch a file in a real repo, and its
    out/<node>.md still lands in the run dir." No gptme/API key available
    in this sandbox (CLAUDE.md Part III: "no network, no agent binary, no
    API key"), so this demonstrates the plumbing with a real subprocess
    (mirroring test_v0_resume.py's rigor) standing in for the agent
    process — not a mocked object, an actual `python3 <script> < prompt`
    invocation going through CommandAgentAdapter's real `cd {workspace} &&
    ...` command construction."""

    def test_writer_patches_repo_and_artifact_lands_in_run_dir(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_str, tempfile.TemporaryDirectory() as runs_str:
            workspace_root = Path(workspace_str).resolve()
            (workspace_root / "app.py").write_text("print('original')\n", encoding="utf-8")

            runs_root = Path(runs_str)
            run_dir = create_run_dir(runs_root, "ship-gate-run")

            node = TaskNode(id="n1", brief="Patch app.py to greet properly.", artifact="out/n1.md", gates=["nonempty"])
            prompt = build_node_prompt(node, run_dir)
            self.assertIn(str(run_dir), prompt)  # sanity: absolute artifact path present

            adapter = CommandAgentAdapter(
                command_template=f"{shlex.quote(sys.executable)} {shlex.quote(str(_FIXTURE_SCRIPT))} < {{prompt_path}}",
                prompt_dir=str(run_dir / "tmp" / "prompts"),
                # This is the load-bearing line: workspace_path is the real
                # repo (work.root), NOT run_dir -- exactly what
                # RecursiveDriver._default_writer_factory now chooses for
                # kind="workspace" (pipeline/driver.py).
                workspace_path=str(workspace_root),
            )

            env = LocalEnvironment(tmp_dir=str(run_dir / "tmp"))
            result = asyncio.run(
                adapter.run_episode(prompt, env, EpisodeBudget(max_duration_seconds=30))
            )
            self.assertEqual(result.status, "done", result.error)

            # (a) The adapter's cwd really was work.root, not run_dir.
            marker = workspace_root / "PATCHED_BY_WRITER.txt"
            self.assertTrue(marker.exists(), "writer never ran inside the workspace root")
            self.assertIn(str(workspace_root), marker.read_text(encoding="utf-8"))

            # (b) out/<node>.md lands under run_dir regardless of that cwd.
            artifact_path = node_artifact_path(run_dir, "n1")
            self.assertTrue(artifact_path.exists())
            self.assertTrue(artifact_path.read_text(encoding="utf-8").strip())
            self.assertTrue(str(artifact_path).startswith(str(run_dir)))


if __name__ == "__main__":
    unittest.main()
