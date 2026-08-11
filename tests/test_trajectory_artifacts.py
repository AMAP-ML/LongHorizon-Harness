from __future__ import annotations

import base64
import json
from pathlib import Path

from lh_harness.trajectory_artifacts import (
    StreamingTrajectoryArtifactWriter,
    persist_trajectory_artifacts,
)


def _codex_image_trajectory(payloads: list[bytes]) -> str:
    return json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "shot-1",
                "type": "mcp_tool_call",
                "result": {
                    "content": [
                        {
                            "type": "image",
                            "data": base64.b64encode(payload).decode("ascii"),
                            "mimeType": "image/png",
                        }
                        for payload in payloads
                    ],
                },
            },
        }
    )


def test_embedded_screenshots_become_files_and_file_references(tmp_path: Path) -> None:
    round_dir = tmp_path / "run" / "round_001"
    payloads = [b"\x89PNG\r\nfirst", b"\x89PNG\r\nsecond"]

    summary = persist_trajectory_artifacts(
        _codex_image_trajectory(payloads),
        round_dir=round_dir,
        role_name="executor",
    )

    assert summary["screenshot_count"] == 2
    manifest = json.loads((round_dir / "executor_screenshots.json").read_text(encoding="utf-8"))
    names = [item["screenshot_file"] for item in manifest["screenshots"]]
    assert names == ["executor_step_0002_01.png", "executor_step_0002_02.png"]
    for name, payload in zip(names, payloads, strict=True):
        assert (round_dir / name).read_bytes() == payload

    assert not (tmp_path / "workspace").exists()

    normalized = (round_dir / "executor_trajectory.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in normalized.splitlines()]
    assert records[-1]["screenshot_file"] == names[0]
    assert records[-1]["screenshot_files"] == names
    assert "data:image" not in normalized
    assert base64.b64encode(payloads[0]).decode("ascii") not in normalized


def test_invalid_inline_image_is_not_written(tmp_path: Path) -> None:
    raw = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "shot-1",
                "type": "mcp_tool_call",
                "result": {"content": [{"type": "image", "data": "not-base64", "mimeType": "image/png"}]},
            },
        }
    )

    summary = persist_trajectory_artifacts(raw, round_dir=tmp_path, role_name="executor")

    assert summary["screenshot_count"] == 0
    assert list(tmp_path.glob("*.png")) == []


def test_streaming_writer_persists_screenshot_before_role_finishes(tmp_path: Path) -> None:
    round_dir = tmp_path / "round_001"
    live_path = round_dir / "executor_raw_trajectory.jsonl"

    writer = StreamingTrajectoryArtifactWriter.from_live_path(live_path)
    assert writer is not None
    writer.consume_line(_codex_image_trajectory([b"\x89PNG\r\nlive"]))

    screenshot = round_dir / "executor_step_0002_01.png"
    assert screenshot.read_bytes() == b"\x89PNG\r\nlive"
    manifest = json.loads((round_dir / "executor_screenshots.json").read_text(encoding="utf-8"))
    assert manifest["live"] is True
    assert manifest["screenshot_count"] == 1
    normalized = (round_dir / "executor_trajectory.jsonl").read_text(encoding="utf-8")
    assert "executor_step_0002_01.png" in normalized
    assert "data:image" not in normalized
