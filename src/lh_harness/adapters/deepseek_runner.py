from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def _emit_result(
    *,
    text: str = "",
    is_error: bool,
    exit_code: int,
    error: str = "",
) -> None:
    record: dict[str, object] = {
        "type": "dsh.result",
        "text": text,
        "is_error": is_error,
        "exit_code": exit_code,
    }
    if error:
        record["error"] = error
    sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _model_patch_path(prompt_path: Path) -> Path:
    return prompt_path.with_name(f"{prompt_path.name}.dsh-model-patch.yml")


# What the positional argument says when the real task rides in the patch
# layer. Deliberately human-readable: if dsh ever stopped preferring the patch
# override, this string is what an agent would receive as its task, and it
# should scream misconfiguration rather than pass as a plausible instruction.
TASK_PLACEHOLDER = "task delivered via --patch"


def run(
    binary: str,
    prompt_path: Path,
    model: str,
    *,
    task_via_patch: bool | None = None,
) -> int:
    """Bridge one episode to the dsh headless runner.

    ``task_via_patch`` picks how the prompt reaches dsh; ``None`` follows the
    platform. It is a parameter rather than a hardcoded ``os.name`` check so
    the test suite exercises both deliveries on both platforms -- the patch
    route is a contract with dsh's layer precedence, and a contract only a
    Windows machine could test would rot quietly.
    """
    if task_via_patch is None:
        # The dsh npm launcher is a .CMD shim on Windows, so the task
        # positional would travel through cmd.exe and its 8191-character
        # command-line limit; role prompts are far larger.
        task_via_patch = os.name == "nt"
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
        patch_lines = [
            "- id: agent-default-model",
            "  config:",
            "    provider: deepseek-official",
            f"    model: {json.dumps(model, ensure_ascii=False)}",
        ]
        if task_via_patch:
            # The headless runner's `task` config normally resolves from the
            # command line, but a later patch layer may override that row with
            # a literal, exactly like the model row above. JSON string escaping
            # is valid YAML, so the prompt stays one line however gnarly it is.
            patch_lines += [
                "- id: headless-runner",
                "  config:",
                f"    task: {json.dumps(prompt, ensure_ascii=False)}",
            ]
        patch_path = _model_patch_path(prompt_path)
        patch_path.write_text("\n".join(patch_lines) + "\n", encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        message = f"could not prepare DeepSeek Harness prompt: {exc}"
        sys.stderr.write(message + "\n")
        _emit_result(is_error=True, exit_code=2, error=message)
        return 2

    command = [
        binary,
        "--profile",
        "headless",
        "--patch",
        os.fspath(patch_path),
        # The config override supplies the real prompt on the patch route; the
        # placeholder only satisfies the runner's non-empty check.
        TASK_PLACEHOLDER if task_via_patch else prompt,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        message = f"could not start DeepSeek Harness binary {binary!r}: {exc}"
        sys.stderr.write(message + "\n")
        _emit_result(is_error=True, exit_code=127, error=message)
        return 127

    _emit_result(
        text=completed.stdout.strip(),
        is_error=completed.returncode != 0,
        exit_code=completed.returncode,
    )
    return completed.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LongHorizon DeepSeek Harness JSONL bridge")
    parser.add_argument("--binary", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--model", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args.binary, Path(args.prompt), args.model)


if __name__ == "__main__":
    raise SystemExit(main())
