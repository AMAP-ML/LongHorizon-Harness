from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from ..environment.base import Environment
from ..runtime_signals import detect_runtime_signals
from ..types import EpisodeBudget, EpisodeResult, ExecResult


class WeaveBenchVMEnvironment(Environment):
    def __init__(
        self,
        raw_env: Any,
        *,
        vm_exec: Callable[..., dict],
        vm_upload: Callable[..., None],
        vm_fetch: Callable[..., bool],
        vm_upload_bytes: Callable[..., None] | None = None,
    ) -> None:
        self.raw_env = raw_env
        self._vm_exec = vm_exec
        self._vm_upload = vm_upload
        self._vm_fetch = vm_fetch
        self._vm_upload_bytes = vm_upload_bytes

    async def exec(self, command: str, timeout: int = 30) -> ExecResult:
        start = time.monotonic()
        result = await asyncio.to_thread(
            self._vm_exec,
            self.raw_env,
            ["bash", "-lc", command],
            timeout=timeout,
        )
        output = result.get("output") or result.get("stdout") or ""
        stderr = result.get("stderr") or ""
        return ExecResult(
            stdout=output,
            stderr=stderr,
            exit_code=int(result.get("returncode") if result.get("returncode") is not None else 0),
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    async def screenshot(self) -> bytes:
        def _capture() -> bytes:
            controller = getattr(self.raw_env, "controller", None)
            if controller is None:
                return b""
            data = controller.get_screenshot()
            return data or b""

        return await asyncio.to_thread(_capture)

    async def upload(self, local_path: str, remote_path: str) -> None:
        path = Path(local_path)
        if self._vm_upload_bytes is not None:
            await asyncio.to_thread(self._vm_upload_bytes, self.raw_env, path, remote_path)
        else:
            await asyncio.to_thread(self._vm_upload, self.raw_env, path.read_text(encoding="utf-8"), remote_path)

    async def download(self, remote_path: str, local_path: str) -> None:
        ok = await asyncio.to_thread(self._vm_fetch, self.raw_env, remote_path, Path(local_path))
        if not ok:
            raise FileNotFoundError(remote_path)


class WeaveBenchEpisodeAdapter:
    def __init__(
        self,
        *,
        output_dir: Path,
        run_once: Callable[[str, Path, EpisodeBudget], dict],
        event_logger: Callable[[str, dict[str, Any]], None] | None = None,
        episode_root_name: str = "episodes",
    ) -> None:
        self.output_dir = Path(output_dir)
        self.episode_root = self.output_dir / "cua_harness" / episode_root_name
        self.run_once = run_once
        self.event_logger = event_logger
        self._episode_index = 0

    async def run_episode(self, prompt: str, env: Environment, budget: EpisodeBudget) -> EpisodeResult:
        del env
        self._episode_index += 1
        ep_dir = self.episode_root / f"ep{self._episode_index:03d}"
        ep_dir.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        self._event(
            "episode_adapter_start",
            {
                "episode": self._episode_index,
                "episode_dir": str(ep_dir),
                "prompt_chars": len(prompt),
                "budget_turns": budget.max_turns,
                "budget_seconds": budget.max_duration_seconds,
            },
        )
        try:
            meta = await asyncio.to_thread(self.run_once, prompt, ep_dir, budget)
            meta_dict = meta if isinstance(meta, dict) else {}
            agent_log_path = ep_dir / "agent.log"
            chat_jsonl_path = ep_dir / "chat.jsonl"
            log_text = _read_text(agent_log_path) + "\n" + _read_text(chat_jsonl_path)
            context_log = _compact_openclaw_log(log_text)
            visible_output = _visible_task_output_from_chat(chat_jsonl_path)
            actions_log = context_log[-200_000:]
            runtime_signals = detect_runtime_signals(log_text)
            diagnostics_only = _diagnostics_only_output(meta_dict, visible_output, runtime_signals)
            duration_ms = int((time.monotonic() - start) * 1000)
            self._event(
                "episode_adapter_done",
                {
                    "episode": self._episode_index,
                    "episode_dir": str(ep_dir),
                    "duration_ms": duration_ms,
                    "agent_log_bytes": len(_read_text(ep_dir / "agent.log").encode("utf-8")),
                    "chat_jsonl_bytes": len(_read_text(ep_dir / "chat.jsonl").encode("utf-8")),
                    "actions_log_bytes": len(actions_log.encode("utf-8")),
                    "actions_log_truncated": len(context_log) > len(actions_log),
                    "actions_log_compacted": context_log != log_text,
                    "tool_activity_observed": _has_tool_activity(log_text),
                    "runtime_signals": runtime_signals,
                    "actions_log_diagnostics_only": diagnostics_only,
                    "meta_keys": sorted(meta.keys()) if isinstance(meta, dict) else [],
                },
            )
            return EpisodeResult(
                status="done",
                actions_log=actions_log,
                duration_ms=duration_ms,
                metadata={
                    **meta_dict,
                    "tool_activity_observed": _has_tool_activity(log_text),
                    "runtime_signals": runtime_signals,
                    "actions_log_truncated": len(context_log) > len(actions_log),
                    "actions_log_compacted": context_log != log_text,
                    "actions_log_diagnostics_only": diagnostics_only,
                    "actions_log_full_chars": len(log_text),
                    "actions_log_context_chars": len(context_log),
                    "task_agent_visible_output": "" if diagnostics_only else visible_output,
                    "episode_dir": str(ep_dir),
                    "agent_log_path": str(agent_log_path),
                    "chat_jsonl_path": str(chat_jsonl_path),
                },
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            self._event(
                "episode_adapter_error",
                {
                    "episode": self._episode_index,
                    "episode_dir": str(ep_dir),
                    "duration_ms": duration_ms,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            return EpisodeResult(
                status="error",
                actions_log=_read_text(ep_dir / "agent.log")[-200_000:],
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=duration_ms,
                metadata={
                    "episode_dir": str(ep_dir),
                    "agent_log_path": str(ep_dir / "agent.log"),
                    "chat_jsonl_path": str(ep_dir / "chat.jsonl"),
                    "actions_log_truncated": len(_read_text(ep_dir / "agent.log")) > 200_000,
                },
            )

    def merge_logs(self) -> None:
        chat_out = self.output_dir / "chat.jsonl"
        log_out = self.output_dir / "agent.log"
        chat_out.parent.mkdir(parents=True, exist_ok=True)
        episodes = 0
        chat_lines = 0
        with chat_out.open("w", encoding="utf-8", errors="ignore") as chat_fh, log_out.open(
            "w", encoding="utf-8", errors="ignore"
        ) as log_fh:
            for ep_dir in sorted(self.episode_root.glob("ep*")):
                episodes += 1
                log_fh.write(f"\n===== {ep_dir.name} agent.log =====\n")
                log_fh.write(_read_text(ep_dir / "agent.log"))
                log_fh.write("\n")
                chat = ep_dir / "chat.jsonl"
                if chat.exists():
                    for line in chat.read_text(encoding="utf-8", errors="ignore").splitlines():
                        if line.strip():
                            chat_fh.write(line + "\n")
                            chat_lines += 1
        self._event(
            "episode_logs_merged",
            {
                "episodes": episodes,
                "chat_lines": chat_lines,
                "chat_jsonl": str(chat_out),
                "agent_log": str(log_out),
            },
        )

    def _event(self, event: str, payload: dict[str, Any]) -> None:
        if self.event_logger is not None:
            self.event_logger(event, payload)


class OpenClawEpisodeAdapter(WeaveBenchEpisodeAdapter):
    """Backward-compatible name for older OpenClaw wrappers.

    New CUA-Harness integrations should import ``WeaveBenchEpisodeAdapter``.
    """


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        return ""


def _diagnostics_only_output(meta: dict[str, Any], visible_output: str, runtime_signals: list[dict[str, str]]) -> bool:
    if visible_output.strip():
        return False
    if runtime_signals:
        return True
    if meta.get("agent_done") is False or meta.get("timed_out"):
        return True
    exit_code = meta.get("exit_code")
    return isinstance(exit_code, int) and exit_code != 0


def _compact_openclaw_log(text: str) -> str:
    parts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("{"):
            parts.append(_clip(_strip_large_payloads(stripped), 4_000))
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            parts.append(_clip(_strip_large_payloads(stripped), 4_000))
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else record
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or record.get("type") or "message")
        if role not in {"assistant", "toolResult", "tool"}:
            continue
        text_part = _message_text_without_images(message)
        tool_calls = _message_tool_calls(message)
        if text_part:
            parts.append(f"[{role}]\n{_clip(_strip_large_payloads(text_part), 12_000)}")
        for call in tool_calls:
            name = str(call.get("name") or call.get("toolName") or call.get("tool") or "tool")
            arguments = call.get("arguments")
            rendered = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
            parts.append(f"[toolCall:{name}] {_clip(_strip_large_payloads(rendered), 4_000)}")
        if role in {"toolResult", "tool"} and not text_part:
            name = str(message.get("toolName") or message.get("name") or "tool")
            parts.append(f"[{role}:{name}] (non-text result omitted)")
    return "\n".join(parts)


def _visible_task_output_from_chat(path: Path) -> str:
    messages: list[str] = []
    assistant_index = 0
    for line in _read_text(path).splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else record
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        assistant_index += 1
        text = _visible_text_from_content(message.get("content")).strip()
        if text:
            messages.append(f"[assistant_step {assistant_index}]\n{text}")
    return "\n\n--- visible assistant output ---\n\n".join(messages)[-200_000:]


def _visible_text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").lower()
        if block_type in {"thinking", "reasoning", "tool_use", "tool-use", "tool_result", "tool-result", "toolcall", "tool_call"}:
            continue
        text = block.get("text") or block.get("content") or block.get("output_text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts)


def _message_text_without_images(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if isinstance(item, str):
            chunks.append(item)
            continue
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if "image" in item_type.lower():
            chunks.append("[image payload omitted; full data is in chat.jsonl]")
            continue
        text = item.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "\n".join(chunks)


def _message_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for key in ("toolCalls", "tool_calls"):
        value = message.get(key)
        if isinstance(value, list):
            calls.extend(item for item in value if isinstance(item, dict))
    content = message.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and str(item.get("type") or "").lower() in {"toolcall", "tool_call", "tool-use", "tool_use"}:
                calls.append(item)
    return calls


def _strip_large_payloads(text: str) -> str:
    text = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "[base64 image omitted]", text)
    return re.sub(r"[A-Za-z0-9+/]{800,}={0,2}", lambda match: f"[large payload omitted chars={len(match.group(0))}]", text)


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 80] + f"\n...[truncated {len(text) - max_chars + 80} chars]..."


def _has_tool_activity(text: str) -> bool:
    return any(
        hint in text
        for hint in (
            "tool_call",
            "toolCall",
            '"toolCalls"',
            '"type":"toolCall"',
            '"type": "toolCall"',
            '"role":"toolResult"',
            '"role": "toolResult"',
            '"toolName"',
            '"role":"tool"',
            "__computer__",
            "shell:",
            "exec",
            "bash",
            "write",
            "edit",
        )
    )
