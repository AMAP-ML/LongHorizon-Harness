"""Durable Dashboard screenshot records for provider role trajectories.

Provider CLIs may embed screenshots as base64 content blocks in their JSONL
stdout. That stream remains useful for diagnostics, but it is not a stable
display protocol. This module materialises screenshots as regular image files
and writes a provider-neutral trajectory containing ``screenshot_file``
references. These files stay in the private run record; they are not copied into
the task Workspace or supplied to the Auditor as completion evidence. The
streaming writer persists each complete event so records survive interruption.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .agent_logs import parse_trajectory
from .supervisor.control_bus import _append_jsonl, _atomic_bytes_write, _open_nofollow


_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DATA_IMAGE_RE = re.compile(
    r"^data:(image/(?:png|jpe?g|webp|gif));base64,([A-Za-z0-9+/=\r\n]+)$",
    re.IGNORECASE,
)
_EXTENSION_BY_MEDIA_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_MAX_SCREENSHOTS = 256
_MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_SCREENSHOT_BYTES = 64 * 1024 * 1024
_MAX_NORMALIZED_STEPS = 5_000


class StreamingTrajectoryArtifactWriter:
    """Persist screenshots as soon as complete provider JSONL records arrive."""

    def __init__(self, *, round_dir: Path, role_name: str) -> None:
        if not _ROLE_RE.fullmatch(role_name):
            raise ValueError(f"invalid trajectory role: {role_name!r}")
        self.round_dir = round_dir
        self.role_name = role_name
        self.step_count = 0
        self.total_bytes = 0
        self.screenshots: list[dict[str, Any]] = []
        self.trajectory_path = round_dir / f"{role_name}_trajectory.jsonl"
        self.manifest_path = round_dir / f"{role_name}_screenshots.json"
        _remove_role_step_images(round_dir, role_name)
        _atomic_bytes_write(self.trajectory_path, b"")
        self._write_manifest(live=True)

    @classmethod
    def from_live_path(cls, path: Path) -> StreamingTrajectoryArtifactWriter | None:
        suffix = "_raw_trajectory"
        if path.suffix != ".jsonl" or not path.stem.endswith(suffix):
            return None
        role_name = path.stem[: -len(suffix)]
        if not re.fullmatch(r"round_\d+", path.parent.name):
            return None
        try:
            return cls(round_dir=path.parent, role_name=role_name)
        except (OSError, ValueError):
            return None

    def consume_line(self, line: bytes | str) -> None:
        if self.step_count >= _MAX_NORMALIZED_STEPS:
            return
        raw = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
        for source_step in parse_trajectory(raw):
            if self.step_count >= _MAX_NORMALIZED_STEPS:
                break
            self.step_count += 1
            step, new_items, new_bytes = _materialize_step(
                source_step,
                step_index=self.step_count,
                round_dir=self.round_dir,
                role_name=self.role_name,
                screenshot_count=len(self.screenshots),
                total_bytes=self.total_bytes,
            )
            _append_jsonl(self.trajectory_path, step)
            if new_items:
                self.screenshots.extend(new_items)
                self.total_bytes += new_bytes
                self._write_manifest(live=True)

    def _write_manifest(self, *, live: bool) -> None:
        manifest = _manifest(self.role_name, self.screenshots, self.total_bytes, live=live)
        _atomic_bytes_write(
            self.manifest_path,
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )


def persist_trajectory_artifacts(
    raw: str,
    *,
    round_dir: Path,
    role_name: str,
) -> dict[str, Any]:
    """Finalize embedded screenshots in the Dashboard's private run record."""

    if not _ROLE_RE.fullmatch(role_name):
        raise ValueError(f"invalid trajectory role: {role_name!r}")

    _remove_role_step_images(round_dir, role_name)
    steps = parse_trajectory(str(raw or ""), max_steps=_MAX_NORMALIZED_STEPS)
    normalized: list[dict[str, Any]] = []
    screenshots: list[dict[str, Any]] = []
    total_bytes = 0

    for step_index, source_step in enumerate(steps, start=1):
        step, new_items, new_bytes = _materialize_step(
            source_step,
            step_index=step_index,
            round_dir=round_dir,
            role_name=role_name,
            screenshot_count=len(screenshots),
            total_bytes=total_bytes,
        )
        screenshots.extend(new_items)
        total_bytes += new_bytes
        normalized.append(step)

    trajectory_name = f"{role_name}_trajectory.jsonl"
    manifest_name = f"{role_name}_screenshots.json"
    trajectory_payload = _jsonl_bytes(normalized)
    manifest = _manifest(role_name, screenshots, total_bytes, live=False)
    manifest_payload = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_bytes_write(round_dir / trajectory_name, trajectory_payload)
    _atomic_bytes_write(round_dir / manifest_name, manifest_payload)

    return {
        "normalized_trajectory": trajectory_name,
        "screenshot_manifest": manifest_name,
        "screenshot_count": len(screenshots),
        "total_screenshot_bytes": total_bytes,
        "screenshots": screenshots,
    }


def _materialize_step(
    source_step: dict[str, Any],
    *,
    step_index: int,
    round_dir: Path,
    role_name: str,
    screenshot_count: int,
    total_bytes: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    step = _strip_inline_images(source_step)
    step["step_num"] = step_index
    files: list[str] = []
    items: list[dict[str, Any]] = []
    written_bytes = 0
    source_images = source_step.get("images")
    if isinstance(source_images, list):
        for image_index, source in enumerate(source_images, start=1):
            if screenshot_count + len(items) >= _MAX_SCREENSHOTS:
                break
            decoded = _decode_data_image(source)
            if decoded is None:
                continue
            media_type, payload = decoded
            if len(payload) > _MAX_SCREENSHOT_BYTES:
                continue
            if total_bytes + written_bytes + len(payload) > _MAX_TOTAL_SCREENSHOT_BYTES:
                continue
            extension = _EXTENSION_BY_MEDIA_TYPE[media_type]
            filename = f"{role_name}_step_{step_index:04d}_{image_index:02d}{extension}"
            digest = hashlib.sha256(payload).hexdigest()
            _atomic_bytes_write(round_dir / filename, payload)
            files.append(filename)
            items.append(
                {
                    "step_num": step_index,
                    "image_index": image_index,
                    "screenshot_file": filename,
                    "media_type": media_type,
                    "bytes": len(payload),
                    "sha256": digest,
                }
            )
            written_bytes += len(payload)
    if files:
        step["has_image"] = True
        step["screenshot_file"] = files[0]
        if len(files) > 1:
            step["screenshot_files"] = files
    return step, items, written_bytes


def _manifest(
    role_name: str,
    screenshots: list[dict[str, Any]],
    total_bytes: int,
    *,
    live: bool,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "role": role_name,
        "trajectory_file": f"{role_name}_trajectory.jsonl",
        "live": live,
        "screenshot_count": len(screenshots),
        "total_screenshot_bytes": total_bytes,
        "screenshots": screenshots,
    }


def _remove_role_step_images(directory: Path, role_name: str) -> None:
    """Remove only image files generated by this exact role protocol."""

    pattern = re.compile(rf"^{re.escape(role_name)}_step_\d{{4}}_\d{{2}}\.(?:png|jpg|webp|gif)$")
    try:
        directory_fd = _open_nofollow(directory, directory=True)
    except OSError:
        return
    try:
        with os.scandir(directory_fd) as entries:
            names = [str(entry.name) for entry in entries if pattern.fullmatch(str(entry.name))]
        for name in names:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                continue
    finally:
        os.close(directory_fd)


def _decode_data_image(value: object) -> tuple[str, bytes] | None:
    if not isinstance(value, str):
        return None
    match = _DATA_IMAGE_RE.fullmatch(value.strip())
    if match is None:
        return None
    media_type = match.group(1).lower()
    if media_type == "image/jpg":
        media_type = "image/jpeg"
    try:
        payload = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
    except (binascii.Error, ValueError):
        return None
    if not payload:
        return None
    return media_type, payload


def _strip_inline_images(value: Any) -> Any:
    """Copy normalized data while ensuring the small JSONL has no data URLs."""

    if isinstance(value, dict):
        return {
            str(key): _strip_inline_images(item)
            for key, item in value.items()
            if key != "images"
        }
    if isinstance(value, list):
        return [_strip_inline_images(item) for item in value]
    if isinstance(value, str) and value.lstrip().lower().startswith("data:image/"):
        return "[screenshot persisted separately]"
    return value


def _jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    if not records:
        return b""
    return ("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n").encode("utf-8")
