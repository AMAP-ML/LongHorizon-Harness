from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from lh_harness import agent_logs
from lh_harness.adapters import deepseek_runner


UNICODE_TEXT = "中文 ≠ → 😀 café\nsecond line"
ENCODINGS = ("ascii", "cp1252", "gbk", "utf-8")


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_result_records_preserve_unicode_and_jsonl_framing(encoding: str) -> None:
    buffer = io.BytesIO()
    with io.TextIOWrapper(buffer, encoding=encoding, errors="strict", newline="\n") as stream:
        with patch.object(sys, "stdout", stream):
            deepseek_runner._emit_result(text=UNICODE_TEXT, is_error=False, exit_code=0)
            deepseek_runner._emit_result(is_error=True, exit_code=9, error=UNICODE_TEXT)
        # Read without flushing here: the bridge must flush each result itself.
        raw = buffer.getvalue().decode(encoding)

    assert raw.endswith("\n")
    records = [json.loads(line) for line in raw.splitlines()]
    assert records == [
        {"type": "dsh.result", "text": UNICODE_TEXT, "is_error": False, "exit_code": 0},
        {"type": "dsh.result", "text": "", "is_error": True, "exit_code": 9, "error": UNICODE_TEXT},
    ]
    assert agent_logs.detect_format(raw) == agent_logs.DEEPSEEK_HARNESS_JSONL
    assert agent_logs.visible_output(raw) == UNICODE_TEXT
    assert agent_logs.assistant_texts(raw) == [UNICODE_TEXT]
    assert agent_logs.parse_trajectory(raw)[0]["text"] == UNICODE_TEXT


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("exit_code", (0, 9))
def test_runner_with_real_encoded_pipes(
    tmp_path: Path, encoding: str, exit_code: int
) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("task", encoding="utf-8")
    # Exercise the bridge in a real Python child, but require no dsh install,
    # credentials, or platform-specific executable stubs.
    script = """
import subprocess
import sys
from unittest.mock import patch
from lh_harness.adapters import deepseek_runner

result = subprocess.CompletedProcess([], int(sys.argv[2]), stdout=sys.argv[3])
with patch.object(deepseek_runner.subprocess, "run", return_value=result):
    code = deepseek_runner.main([
        "--binary", "fake-dsh", "--prompt", sys.argv[1], "--model", "fake-model",
    ])
raise SystemExit(code)
"""
    env = {**os.environ, "PYTHONIOENCODING": f"{encoding}:strict", "PYTHONUTF8": "0"}
    completed = subprocess.run(
        [sys.executable, "-c", script, str(prompt_path), str(exit_code), UNICODE_TEXT],
        env=env,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == exit_code, completed.stderr
    assert completed.stderr == b""
    record = json.loads(completed.stdout)
    assert record == {
        "type": "dsh.result",
        "text": UNICODE_TEXT,
        "is_error": exit_code != 0,
        "exit_code": exit_code,
    }


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("failure", ("prepare", "start"))
def test_unicode_diagnostics_do_not_hide_structured_errors(
    tmp_path: Path, encoding: str, failure: str
) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("task", encoding="utf-8")
    stdout_buffer = io.BytesIO()
    stderr_buffer = io.BytesIO()
    if failure == "prepare":
        fail = patch.object(Path, "read_text", side_effect=OSError(UNICODE_TEXT))
        expected_code = 2
        expected_message = f"could not prepare DeepSeek Harness prompt: {UNICODE_TEXT}"
    else:
        fail = patch.object(deepseek_runner.subprocess, "run", side_effect=OSError(UNICODE_TEXT))
        expected_code = 127
        expected_message = f"could not start DeepSeek Harness binary 'fake-dsh': {UNICODE_TEXT}"

    with (
        io.TextIOWrapper(stdout_buffer, encoding=encoding, errors="strict", newline="\n") as stdout,
        io.TextIOWrapper(stderr_buffer, encoding=encoding, errors="strict", newline="\n") as stderr,
    ):
        with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr), fail:
            code = deepseek_runner.run("fake-dsh", prompt_path, "fake-model")
        stderr.flush()
        raw = stdout_buffer.getvalue().decode(encoding)
        diagnostic = stderr_buffer.getvalue().decode(encoding)

    assert code == expected_code
    assert json.loads(raw) == {
        "type": "dsh.result", "text": "", "is_error": True,
        "exit_code": expected_code, "error": expected_message,
    }
    if encoding == "utf-8":
        assert diagnostic == expected_message + "\n"
    else:
        assert diagnostic == expected_message.encode("ascii", "backslashreplace").decode("ascii") + "\n"
