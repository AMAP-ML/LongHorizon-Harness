"""The work object (PLAN.md §A3, §B1) — the single input abstraction that
replaces ``source.txt``-only. Three kinds:

- ``"text"``   — today's corpus-in-a-string mode, unchanged (§4.2).
- ``"workspace"`` — a real directory a Writer's cwd becomes directly (a repo,
  a folder of notes). Never copied into the run dir (§A3 point 2): a
  workspace unit is a set of real paths, and materializing them the way
  ``v2/survey.py:materialize_units`` does for corpus mode would double a
  repo on disk and immediately desynchronize from it.
- ``"none"``   — a goal with no input at all. Legal, degenerate; nothing in
  this module measures it because there is nothing to measure.

Measurement (``measure_workspace``) is deterministic and model-free — pure
filesystem + stdlib, per §A3's "must never require reading file contents
into a model." It reuses ``v1/gates.estimate_tokens`` (the same
words/0.75 heuristic every other token count in this repo uses) rather
than inventing a second approximation.

**Gitignore support is deliberately minimal**, not a full spec
implementation (this repo is stdlib-only, pyproject.toml): exact/glob
patterns via ``fnmatch``, a trailing ``/`` for directory-only patterns, and
patterns anchored to their own ``.gitignore``'s directory when they contain
an internal ``/`` (matched elsewhere in the tree otherwise). ``!`` negation
lines are recognized and skipped, not honored — a false-exclude here just
means one more denylist entry than git itself would apply, which is the
safe direction to be wrong in for a harness deciding what a Writer may
read, not the dangerous one.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Iterator, Literal

from ..v1.gates import estimate_tokens

if TYPE_CHECKING:
    from ..v2.survey import SpineUnit

# §A3: "anything over a size ceiling" is excluded from measurement before
# counting -- a single huge generated/vendored file must not dominate
# est_tokens the way it would dominate a naive walk.
DEFAULT_MAX_FILE_BYTES = 1_000_000  # 1MB

# A workspace SpineUnit (survey_workspace, below) is capped the same way
# corpus-mode units are floored (v2/survey.py's DEFAULT_MIN_UNIT_TOKENS) --
# here the direction is the opposite: split an over-large directory's files
# across multiple units rather than merge undersized ones, so no single
# unit's members overflow a leaf's token budget before a Writer even opens
# them.
DEFAULT_UNIT_MAX_TOKENS = 8_000

_SNIFF_BYTES = 8192

# Directories that are never workspace content, regardless of .gitignore --
# VCS internals, dependency trees, and build output. ".kusudaemon" is here
# too: the default --workspace runs_root nests the run directory (and hence
# every OTHER node's artifacts) inside the repo itself (PLAN.md §A3 point
# 1), and it must never be walked as if it were task content.
_BUILTIN_DENY_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".kusudaemon",
        "node_modules", "dist", "build", "target", "out",
        "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
        ".next", ".nuxt", ".cache", ".idea",
    }
)

# Lockfiles: high byte count, ~zero judgment-relevant content, and never
# something a Writer should be reading as "the corpus."
_BUILTIN_DENY_FILENAMES = frozenset(
    {
        "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
        "Cargo.lock", "poetry.lock", "Pipfile.lock", "composer.lock",
        "Gemfile.lock", "go.sum",
    }
)

_BINARY_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
        ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
        ".exe", ".dll", ".so", ".dylib", ".a", ".o", ".class", ".jar",
        ".pyc", ".pyo", ".woff", ".woff2", ".ttf", ".otf", ".eot",
        ".mp3", ".mp4", ".mov", ".avi", ".wav", ".flac", ".ogg",
        ".db", ".sqlite", ".sqlite3", ".bin", ".wasm",
    }
)


@dataclass(frozen=True)
class WorkObject:
    """PLAN.md §A3's exact schema. ``files``/``bytes``/``est_tokens``/
    ``top_dirs`` are measured once, by code, at construction — no model
    call, ever (that's the whole point: it feeds the cheap half of tier
    classification, §A4, unbuilt as of this module)."""

    kind: Literal["text", "workspace", "none"]
    root: Path | None  # workspace: the directory agents actually work in
    text_path: Path | None  # text: the corpus file, if it came from one
    include: tuple[str, ...]  # globs, default ("**/*",)
    exclude: tuple[str, ...]  # user-supplied globs; NOT gitignore/denylist
    files: int
    bytes: int
    est_tokens: int
    top_dirs: tuple[tuple[str, int], ...]  # (path, est_tokens), largest first


def work_object_none() -> WorkObject:
    """§A3 point 3: a goal with no input at all is legal, not degenerate.
    Nothing here needs measuring; this is just the canonical empty value so
    callers don't hand-roll the all-zero constructor at each call site."""
    return WorkObject(
        kind="none",
        root=None,
        text_path=None,
        include=(),
        exclude=(),
        files=0,
        bytes=0,
        est_tokens=0,
        top_dirs=(),
    )


def work_object_from_text(
    source_text: str, *, text_path: str | Path | None = None
) -> WorkObject:
    """``kind="text"`` from a legacy ``source_text`` string — the
    deprecated-alias path (PLAN.md §A3: "``RunOptions`` ... keeps
    ``source_text`` as a deprecated alias that constructs a ``kind="text"``
    object"). Measures the in-memory string directly and never touches
    disk, so this is safe to call before ``source.txt`` even exists (driver
    construction time, ``pipeline/driver.py``'s ``RunOptions`` still being
    the thing that eventually writes it via ``_write_source_and_spec`` —
    unmodified by this function). ``text_path`` is optional provenance (the
    ``--source @file`` the text came from, if any); the pipeline today
    never threads a path through past ``_read_text_arg``, so most callers
    leave it ``None``.

    ``top_dirs`` is always empty: a flat text blob has no directory
    structure to report."""
    text = source_text or ""
    return WorkObject(
        kind="text",
        root=None,
        text_path=Path(text_path) if text_path is not None else None,
        include=("**/*",),
        exclude=(),
        files=1 if text.strip() else 0,
        bytes=len(text.encode("utf-8")),
        est_tokens=estimate_tokens(text),
        top_dirs=(),
    )


@dataclass(frozen=True)
class _GitignoreRule:
    pattern: str
    dir_only: bool
    anchored: bool
    base_dir: Path


def _parse_gitignore(path: Path) -> list[_GitignoreRule]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    base_dir = path.parent
    rules: list[_GitignoreRule] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("!"):
            # Negation, deliberately unsupported (module docstring) -- skip
            # rather than mis-apply it as a positive exclude.
            continue
        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1]
        stripped = line.strip("/")
        anchored = "/" in stripped
        pattern = line.lstrip("/")
        if not pattern:
            continue
        rules.append(
            _GitignoreRule(pattern=pattern, dir_only=dir_only, anchored=anchored, base_dir=base_dir)
        )
    return rules


def _rule_matches(rule: _GitignoreRule, path: Path, *, is_dir: bool) -> bool:
    if rule.dir_only and not is_dir:
        return False
    try:
        rel = path.relative_to(rule.base_dir)
    except ValueError:
        return False
    posix = rel.as_posix()
    if rule.anchored:
        return fnmatch.fnmatch(posix, rule.pattern)
    return any(fnmatch.fnmatch(part, rule.pattern) for part in rel.parts)


def _gitignore_excludes(rules: tuple[_GitignoreRule, ...], path: Path, *, is_dir: bool) -> bool:
    return any(_rule_matches(rule, path, is_dir=is_dir) for rule in rules)


def _glob_matches(rel_posix: str, patterns: tuple[str, ...]) -> bool:
    """Common-case glob matching, not a full spec implementation (module
    docstring). ``"**/*"``/``"**"`` (the ``include`` default) always match --
    fnmatch's own ``*`` -> ``.*`` translation requires a literal ``/`` in
    the candidate for a two-star-then-slash pattern to match a root-level
    file, which would wrongly exclude every file not inside a
    subdirectory. Beyond that: a whole-path fnmatch, then a
    name-only fnmatch (so ``"*.py"`` matches at any depth, the common
    single-star case), then a bare ``"**/"`` prefix stripped and retried."""
    for pattern in patterns:
        if pattern in ("**/*", "**"):
            return True
        if fnmatch.fnmatch(rel_posix, pattern):
            return True
        if fnmatch.fnmatch(PurePosixPath(rel_posix).name, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(rel_posix, pattern[3:]):
            return True
    return False


def _looks_binary(path: Path) -> bool:
    if path.suffix.lower() in _BINARY_EXTENSIONS:
        return True
    try:
        with path.open("rb") as fh:
            chunk = fh.read(_SNIFF_BYTES)
    except OSError:
        return True
    return b"\x00" in chunk


def _top_level_dir(rel: PurePosixPath) -> str:
    # First path segment only (not two levels): a single top-level grouping
    # is enough signal for tier classification's "where are the tokens"
    # question (§A3), and it keeps top_dirs' cardinality bounded by a
    # repo's top-level layout rather than by every nested package.
    parts = rel.parts
    return parts[0] if len(parts) > 1 else "."


def _iter_included_files(
    root: Path, include: tuple[str, ...], exclude: tuple[str, ...]
) -> Iterator[tuple[Path, str, int, int]]:
    """Yield ``(abs_path, rel_posix, tokens, size)`` for every file under
    ``root`` that survives: the builtin deny lists, every ``.gitignore``
    encountered on the way down (rules accumulate with depth, same as git's
    own resolution order), the ``include``/``exclude`` globs, the size
    ceiling, and the binary heuristic. One walk, shared by
    ``measure_workspace`` and ``survey_workspace`` so they can never
    disagree about what counts as "in the workspace."
    """
    root = Path(root).resolve()
    accumulated: dict[Path, tuple[_GitignoreRule, ...]] = {root: ()}
    for dirpath_str, dirnames, filenames in os.walk(root):
        dirpath = Path(dirpath_str)
        rules_here = accumulated.get(dirpath, ())
        gitignore_file = dirpath / ".gitignore"
        if gitignore_file.is_file():
            rules_here = rules_here + tuple(_parse_gitignore(gitignore_file))

        kept_dirs = []
        for dirname in sorted(dirnames):
            if dirname in _BUILTIN_DENY_DIRS:
                continue
            child = dirpath / dirname
            if _gitignore_excludes(rules_here, child, is_dir=True):
                continue
            kept_dirs.append(dirname)
            accumulated[child] = rules_here
        dirnames[:] = kept_dirs

        for filename in sorted(filenames):
            if filename in _BUILTIN_DENY_FILENAMES:
                continue
            path = dirpath / filename
            rel_posix = path.relative_to(root).as_posix()
            if not _glob_matches(rel_posix, include):
                continue
            if exclude and _glob_matches(rel_posix, exclude):
                continue
            if _gitignore_excludes(rules_here, path, is_dir=False):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > DEFAULT_MAX_FILE_BYTES:
                continue
            if _looks_binary(path):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            yield path, rel_posix, estimate_tokens(text), size


def measure_workspace(
    root: str | Path,
    include: tuple[str, ...] = ("**/*",),
    exclude: tuple[str, ...] = (),
) -> WorkObject:
    """§A3/§B1: deterministic, model-free measurement of a real directory.
    Every file counted here is one ``_iter_included_files`` already decided
    is real workspace content -- not binary, not denylisted, not
    gitignored, not over the size ceiling."""
    root = Path(root).resolve()
    files = 0
    total_bytes = 0
    total_tokens = 0
    dir_tokens: dict[str, int] = {}
    for _path, rel_posix, tokens, size in _iter_included_files(root, include, exclude):
        files += 1
        total_bytes += size
        total_tokens += tokens
        top = _top_level_dir(PurePosixPath(rel_posix))
        dir_tokens[top] = dir_tokens.get(top, 0) + tokens
    top_dirs = tuple(sorted(dir_tokens.items(), key=lambda kv: (-kv[1], kv[0])))
    return WorkObject(
        kind="workspace",
        root=root,
        text_path=None,
        include=tuple(include),
        exclude=tuple(exclude),
        files=files,
        bytes=total_bytes,
        est_tokens=total_tokens,
        top_dirs=top_dirs,
    )


def iter_workspace_paths(work: WorkObject, *, limit: int = 300) -> list[str]:
    """Cheap, content-free listing of up to ``limit`` relative paths under a
    ``kind="workspace"`` WorkObject (PLAN.md §A4.2/§B2: a scope estimate's
    "truncated file-tree outline" digest). Deliberately *not*
    ``_iter_included_files`` — that walk reads every file's bytes to
    compute ``est_tokens``, which is the right cost for measuring a
    WorkObject once but wasteful just to list paths for a prompt digest.
    This only respects the builtin deny-dirs/filenames (not
    ``.gitignore``/``include``/``exclude``) — a coarse outline, not a second
    source of truth for "what counts as workspace content"; that stays
    ``_iter_included_files``'s job alone."""
    if work.kind != "workspace" or work.root is None:
        return []
    root = work.root
    paths: list[str] = []
    for dirpath_str, dirnames, filenames in os.walk(root):
        dirpath = Path(dirpath_str)
        dirnames[:] = sorted(d for d in dirnames if d not in _BUILTIN_DENY_DIRS)
        for filename in sorted(filenames):
            if filename in _BUILTIN_DENY_FILENAMES:
                continue
            rel = (dirpath / filename).relative_to(root).as_posix()
            paths.append(rel)
            if len(paths) >= limit:
                return paths
    return paths


def survey_workspace(
    work: WorkObject, *, max_unit_tokens: int = DEFAULT_UNIT_MAX_TOKENS
) -> list[SpineUnit]:
    """Workspace survey (§B1's one-line description: "gitignore-aware walk,
    group by directory, respect a size ceiling"). Zero model calls, same
    mechanism ``measure_workspace`` uses (``_iter_included_files``), just
    returning ``SpineUnit``s instead of aggregate counts.

    Units group by top-level directory (matching ``top_dirs``'s own
    grouping choice above); a group whose combined tokens exceed
    ``max_unit_tokens`` is split into multiple numbered units by simple
    greedy bin-packing over its sorted member list, so no single unit's
    members can overflow a leaf's token budget before a Writer even opens
    them.

    Returns ``SpineUnit``s with ``members`` populated and
    ``start_chunk=end_chunk=-1`` — corpus mode's only two consumers of
    those fields, ``assemble_spine``/``materialize_units``, are a parallel
    path (§A3 point 2: a workspace is never copied into the run dir) and
    must never be called on these units; -1 is a sentinel precisely because
    it can never be a valid chunk index, so a caller that accidentally
    mixes the two paths gets an out-of-range index, not a silent wrong
    slice.

    Local import of ``SpineUnit`` (not ``v2`` -> ``v6``) keeps the existing
    v0-v5 dependency direction intact -- v6 depends on v2's schema, not the
    other way around.
    """
    from ..v2.survey import SpineUnit

    if work.kind != "workspace" or work.root is None:
        raise ValueError(f"survey_workspace needs a kind='workspace' WorkObject with a root, got kind={work.kind!r}")

    groups: dict[str, list[tuple[str, int]]] = {}
    for _path, rel_posix, tokens, _size in _iter_included_files(work.root, work.include, work.exclude):
        top = _top_level_dir(PurePosixPath(rel_posix))
        groups.setdefault(top, []).append((rel_posix, tokens))

    units = []
    unit_index = 0
    for dir_name in sorted(groups):
        entries = sorted(groups[dir_name])
        chunks = _chunk_by_token_ceiling(entries, max_unit_tokens)
        multi = len(chunks) > 1
        label_base = dir_name if dir_name != "." else "(repo root)"
        for part_index, chunk in enumerate(chunks, start=1):
            unit_index += 1
            label = f"{label_base} (part {part_index})" if multi else label_base
            units.append(
                SpineUnit(
                    id=f"unit-{unit_index:02d}",
                    label=label,
                    start_chunk=-1,
                    end_chunk=-1,
                    tokens=sum(tokens for _rel, tokens in chunk),
                    members=tuple(rel for rel, _tokens in chunk),
                )
            )
    return units


def _chunk_by_token_ceiling(
    entries: list[tuple[str, int]], max_tokens: int
) -> list[list[tuple[str, int]]]:
    chunks: list[list[tuple[str, int]]] = []
    current: list[tuple[str, int]] = []
    current_tokens = 0
    for rel, tokens in entries:
        if current and current_tokens + tokens > max_tokens:
            chunks.append(current)
            current, current_tokens = [], 0
        current.append((rel, tokens))
        current_tokens += tokens
    if current:
        chunks.append(current)
    return chunks
