"""Compile + repair gate (PLAN.md §4.6.3): "Run latexmk; exit code and log
are the gate."

The harness is corpus-agnostic (§1: "must work on a textbook... a folder of
unstructured personal lecture notes... without special-casing") and most
corpora it runs over don't compile anything — there's no LaTeX toolchain to
assume. So the compile command is a plain injected string, exactly like
§12's provider transport is an injectable callable "so tests never need a
real network call": a caller assembling a LaTeX doc passes
``"latexmk -pdf -interaction=nonstopmode main.tex"``; a caller assembling
plain markdown notes passes nothing and the gate trivially passes — there is
nothing to compile, so there is nothing to fail.

Runs through the existing ``Environment.exec`` abstraction (local/ssh/docker,
already used for every episode dispatch) rather than a fresh ``subprocess``
call, so compile behaves the same way under whichever environment the run
is using.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from pathlib import Path

from ..environment.base import Environment
from .run_dir import assembly_dir, compile_log_path

DEFAULT_COMPILE_TIMEOUT_SECONDS = 120


@dataclass
class CompileResult:
    passed: bool
    exit_code: int
    log: str
    skipped: bool = False


async def run_compile(
    run_dir: str | Path,
    env: Environment,
    compile_command: str | None,
    *,
    cwd: str | Path | None = None,
    timeout: int = DEFAULT_COMPILE_TIMEOUT_SECONDS,
) -> CompileResult:
    run_dir = Path(run_dir)
    if not compile_command:
        result = CompileResult(passed=True, exit_code=0, log="", skipped=True)
        compile_log_path(run_dir).write_text("(no compile_command configured — skipped)\n", encoding="utf-8")
        return result

    work_dir = Path(cwd) if cwd is not None else assembly_dir(run_dir)
    shell_command = f"cd {shlex.quote(str(work_dir))} && {compile_command}"
    exec_result = await env.exec(shell_command, timeout=timeout, tee_path=str(compile_log_path(run_dir)))

    log = exec_result.stdout
    if exec_result.stderr:
        log = f"{log}\n--- stderr ---\n{exec_result.stderr}" if log else exec_result.stderr
    log = f"$ {compile_command}\n({time.strftime('%Y-%m-%dT%H:%M:%S')})\n\n{log}"
    compile_log_path(run_dir).write_text(log, encoding="utf-8")

    return CompileResult(passed=exec_result.exit_code == 0, exit_code=exec_result.exit_code, log=log)
