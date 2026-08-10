from __future__ import annotations

import json
import re
import shlex
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from harbor.environments.base import BaseEnvironment

from harness_types import EpisodeBudget, EpisodeResult
from runtime_signals import detect_runtime_signals

from .common import redact_secrets
from .constants import CLAUDE_CODE_PROXY_REMOTE, VERIFIER_ROLES
from .environment import (
    HarborEnvironmentAdapter,
    workspace_snapshot,
    workspace_snapshot_diff,
)


REMOTE_RUNTIME_ROOT = "/tmp/cua_harness_claudecode"
COMMAND_TIMEOUT_RE = re.compile(
    r"\bCommand timed out after\s+\d+(?:\.\d+)?\s*(?:seconds?|s)?\b",
    re.IGNORECASE,
)


class HarborClaudeCodeEpisodeAdapter:
    """Runs one CUA-Harness role episode through Claude Code in Harbor."""

    def __init__(
        self,
        *,
        harbor_environment: BaseEnvironment,
        environment_adapter: HarborEnvironmentAdapter,
        output_dir: Path,
        role: str,
        model: str,
        api_key: str | None,
        base_url: str | None,
        effort: str | None,
        workspace_path: str,
        disallowed_tools: str,
        installed_request_proxy: bool,
        sampling_config: dict[str, Any] | None = None,
        dashscope_wait_timeout_sec: int | None = None,
        event_logger: Any | None = None,
    ) -> None:
        self.harbor_environment = harbor_environment
        self.environment_adapter = environment_adapter
        self.output_dir = Path(output_dir)
        self.role = role
        self.model = model
        self.api_key = api_key
        self.base_url = _anthropic_base_url(base_url)
        self.effort = effort
        self.workspace_path = workspace_path.rstrip("/") or "/app"
        self.disallowed_tools = disallowed_tools
        self.installed_request_proxy = installed_request_proxy
        self.sampling_config = dict(sampling_config or {})
        self.dashscope_wait_timeout_sec = dashscope_wait_timeout_sec
        self.event_logger = event_logger
        self.episode_root = self.output_dir / "episodes" / role
        self.episode_index = 0
        self.usage_totals = {
            "n_input_tokens": 0,
            "n_output_tokens": 0,
            "n_cache_tokens": 0,
        }

    async def run_episode(
        self,
        prompt: str,
        env: HarborEnvironmentAdapter,
        budget: EpisodeBudget,
    ) -> EpisodeResult:
        del env
        self.episode_index += 1
        ep_dir = self.episode_root / f"ep{self.episode_index:03d}"
        ep_dir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()

        prompt_path = ep_dir / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        remote_dir = (
            f"{REMOTE_RUNTIME_ROOT}/{self.role}_{self.episode_index:03d}_"
            f"{int(time.time() * 1000)}"
        )
        remote_prompt = f"{remote_dir}/prompt.md"
        await self.environment_adapter.upload(str(prompt_path), remote_prompt)
        before_snapshot = await workspace_snapshot(
            self.harbor_environment,
            self.workspace_path,
        ) if self.role in VERIFIER_ROLES else None

        self._event(
            "episode_start",
            {
                "role": self.role,
                "episode": self.episode_index,
                "prompt_chars": len(prompt),
                "budget_turns": budget.max_turns,
                "budget_seconds": budget.max_duration_seconds,
                "sampling_config": self.sampling_config,
            },
        )

        command = self._render_command(
            remote_dir=remote_dir,
            remote_prompt=remote_prompt,
            max_turns=budget.max_turns,
        )
        timeout = max(1, int(budget.max_duration_seconds)) + 60
        try:
            result = await self.harbor_environment.exec(
                command=command,
                cwd=self.workspace_path,
                env=self._command_env(remote_dir),
                timeout_sec=timeout,
            )
        except Exception as exc:
            if not _is_command_timeout_exception(exc):
                raise
            return await self._timeout_episode_result(
                started=started,
                ep_dir=ep_dir,
                prompt_path=prompt_path,
                remote_dir=remote_dir,
                budget=budget,
                command_timeout_sec=timeout,
                before_snapshot=before_snapshot,
                message=f"{type(exc).__name__}: {exc}",
            )

        if _is_command_timeout_result(result):
            return await self._timeout_episode_result(
                started=started,
                ep_dir=ep_dir,
                prompt_path=prompt_path,
                remote_dir=remote_dir,
                budget=budget,
                command_timeout_sec=timeout,
                before_snapshot=before_snapshot,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                exit_code=result.return_code,
                message=(result.stderr or result.stdout or "Claude Code role episode timed out"),
            )

        proxy_log_paths = await self._persist_proxy_logs(remote_dir, ep_dir)

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        after_snapshot = await workspace_snapshot(
            self.harbor_environment,
            self.workspace_path,
        ) if before_snapshot is not None else None
        workspace_diff = (
            workspace_snapshot_diff(before_snapshot, after_snapshot)
            if before_snapshot is not None and after_snapshot is not None
            else None
        )
        combined = stdout + ("\n" if stdout and stderr else "") + stderr
        redacted_stdout = redact_secrets(stdout, [self.api_key])
        redacted_stderr = redact_secrets(stderr, [self.api_key])
        redacted_combined = redact_secrets(combined, [self.api_key])
        (ep_dir / "claude_stream.jsonl").write_text(redacted_stdout, encoding="utf-8", errors="ignore")
        (ep_dir / "stderr.log").write_text(redacted_stderr, encoding="utf-8", errors="ignore")
        (ep_dir / "agent.log").write_text(redacted_combined, encoding="utf-8", errors="ignore")

        visible_output = _extract_claude_visible_output(stdout) or _extract_claude_visible_output(combined)
        runtime_signals = detect_runtime_signals(combined)
        duration_ms = int((time.monotonic() - started) * 1000)
        steps_capped = _claude_steps_capped(stdout, budget.max_turns)
        status = "done" if result.return_code == 0 else "error"
        usage_metadata = _usage_metadata(stdout)
        self._record_usage(usage_metadata)

        metadata = {
            "agent_done": result.return_code == 0,
            "role": self.role,
            "episode_dir": str(ep_dir),
            "prompt_path": str(prompt_path),
            "agent_log_path": str(ep_dir / "agent.log"),
            "claude_stream_path": str(ep_dir / "claude_stream.jsonl"),
            "exit_code": result.return_code,
            "steps_capped": steps_capped,
            "runtime_signals": runtime_signals,
            "assistant_visible_output": visible_output,
            "output_text": visible_output,
            "actions_log_truncated": len(redacted_combined) > 200_000,
            "model": self.model,
            "effort": self.effort,
            "sampling_config": self.sampling_config,
            "proxy_log_paths": proxy_log_paths,
            **usage_metadata,
        }
        if self.role in VERIFIER_ROLES:
            metadata.update(
                {
                    "verifier_workspace_snapshot_enabled": before_snapshot is not None,
                    "verifier_workspace_mutation_detected": bool(
                        workspace_diff and workspace_diff["detected"]
                    ),
                    "verifier_workspace_mutation_counts": (
                        workspace_diff["counts"] if workspace_diff else {}
                    ),
                    "verifier_workspace_mutations": (
                        workspace_diff["mutations"] if workspace_diff else {}
                    ),
                    "verifier_workspace_restore_on_mutation": True,
                    "verifier_workspace_restored": False,
                }
            )

        self._event(
            "episode_done",
            {
                "role": self.role,
                "episode": self.episode_index,
                "status": status,
                "exit_code": result.return_code,
                "duration_ms": duration_ms,
                "visible_output_chars": len(visible_output),
                "runtime_signals": runtime_signals,
                "proxy_log_paths": proxy_log_paths,
                "workspace_mutation_detected": bool(
                    workspace_diff and workspace_diff["detected"]
                ),
            },
        )

        return EpisodeResult(
            status=status,
            actions_log=redacted_combined[-200_000:],
            error=redacted_stderr[-4000:] if result.return_code != 0 else None,
            duration_ms=duration_ms,
            metadata=metadata,
        )

    async def _timeout_episode_result(
        self,
        *,
        started: float,
        ep_dir: Path,
        prompt_path: Path,
        remote_dir: str,
        budget: EpisodeBudget,
        command_timeout_sec: int,
        before_snapshot: dict[str, str] | None,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        message: str = "",
    ) -> EpisodeResult:
        proxy_log_paths = await self._safe_persist_proxy_logs(remote_dir, ep_dir)
        timeout_summary = (
            f"Claude Code role episode exceeded {budget.max_duration_seconds}s "
            f"(command timeout_sec={command_timeout_sec})."
        )
        timeout_evidence = "\n".join(
            part.strip()
            for part in (message, timeout_summary)
            if part and part.strip()
        )
        combined = "\n".join(
            part
            for part in (stdout, stderr, timeout_evidence)
            if part
        )

        redacted_stdout = redact_secrets(stdout, [self.api_key])
        redacted_stderr = redact_secrets(stderr, [self.api_key])
        redacted_combined = redact_secrets(combined, [self.api_key])
        redacted_timeout = redact_secrets(timeout_evidence, [self.api_key])
        (ep_dir / "claude_stream.jsonl").write_text(
            redacted_stdout,
            encoding="utf-8",
            errors="ignore",
        )
        (ep_dir / "stderr.log").write_text(
            redacted_stderr or redacted_timeout,
            encoding="utf-8",
            errors="ignore",
        )
        (ep_dir / "agent.log").write_text(
            redacted_combined,
            encoding="utf-8",
            errors="ignore",
        )

        after_snapshot = None
        workspace_diff = None
        snapshot_error = None
        if before_snapshot is not None:
            try:
                after_snapshot = await workspace_snapshot(
                    self.harbor_environment,
                    self.workspace_path,
                )
            except Exception as exc:
                snapshot_error = f"{type(exc).__name__}: {exc}"
            if after_snapshot is not None:
                workspace_diff = workspace_snapshot_diff(before_snapshot, after_snapshot)

        visible_output = (
            _extract_claude_visible_output(stdout)
            or _extract_claude_visible_output(combined)
        )
        runtime_signals = detect_runtime_signals(combined)
        runtime_signals.append(
            {
                "signal": "episode_timeout",
                "evidence": redacted_timeout[-500:],
            }
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        usage_metadata = _usage_metadata(stdout)
        self._record_usage(usage_metadata)

        metadata = {
            "agent_done": False,
            "role": self.role,
            "episode_dir": str(ep_dir),
            "prompt_path": str(prompt_path),
            "agent_log_path": str(ep_dir / "agent.log"),
            "claude_stream_path": str(ep_dir / "claude_stream.jsonl"),
            "exit_code": exit_code,
            "steps_capped": _claude_steps_capped(stdout, budget.max_turns),
            "runtime_signals": runtime_signals,
            "assistant_visible_output": visible_output,
            "output_text": visible_output,
            "actions_log_truncated": len(redacted_combined) > 200_000,
            "model": self.model,
            "effort": self.effort,
            "sampling_config": self.sampling_config,
            "proxy_log_paths": proxy_log_paths,
            "timeout_kind": "command_timeout",
            "timeout_sec": command_timeout_sec,
            "budget_seconds": budget.max_duration_seconds,
            **usage_metadata,
        }
        if self.role in VERIFIER_ROLES:
            metadata.update(
                {
                    "verifier_workspace_snapshot_enabled": before_snapshot is not None,
                    "verifier_workspace_mutation_detected": bool(
                        workspace_diff and workspace_diff["detected"]
                    ),
                    "verifier_workspace_mutation_counts": (
                        workspace_diff["counts"] if workspace_diff else {}
                    ),
                    "verifier_workspace_mutations": (
                        workspace_diff["mutations"] if workspace_diff else {}
                    ),
                    "verifier_workspace_restore_on_mutation": True,
                    "verifier_workspace_restored": False,
                }
            )
            if snapshot_error is not None:
                metadata["verifier_workspace_snapshot_error"] = snapshot_error

        self._event(
            "episode_done",
            {
                "role": self.role,
                "episode": self.episode_index,
                "status": "timeout",
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "visible_output_chars": len(visible_output),
                "runtime_signals": runtime_signals,
                "proxy_log_paths": proxy_log_paths,
                "workspace_mutation_detected": bool(
                    workspace_diff and workspace_diff["detected"]
                ),
                "timeout_kind": "command_timeout",
            },
        )

        return EpisodeResult(
            status="timeout",
            actions_log=redacted_combined[-200_000:],
            error=redacted_timeout[-4000:],
            duration_ms=duration_ms,
            metadata=metadata,
        )

    async def _safe_persist_proxy_logs(self, remote_dir: str, ep_dir: Path) -> dict[str, str]:
        try:
            return await self._persist_proxy_logs(remote_dir, ep_dir)
        except Exception as exc:
            self._event(
                "proxy_log_download_failed",
                {
                    "role": self.role,
                    "episode": self.episode_index,
                    "remote_dir": remote_dir,
                    "error": repr(exc),
                },
            )
            return {}

    def _render_command(self, *, remote_dir: str, remote_prompt: str, max_turns: int) -> str:
        max_turn_flags = ""
        if max_turns and max_turns > 0:
            max_turn_flags = f" --max-turns {int(max_turns)}"

        flags = (
            "claude --verbose --output-format=stream-json "
            "--permission-mode=bypassPermissions "
            f"{self._effort_flag()}"
            f"--model {shlex.quote(self.model)}"
            f"{max_turn_flags} "
            f"--disallowedTools {shlex.quote(self.disallowed_tools)} "
            "--print"
        )
        quoted_prompt = shlex.quote(remote_prompt)
        quoted_config = shlex.quote(f"{remote_dir}/claude_config")
        proxy_prefix = self._render_proxy_prefix(remote_dir)
        return (
            "set -u; "
            'export PATH="$HOME/.local/bin:$PATH"; '
            f"export CLAUDE_CONFIG_DIR={quoted_config}; "
            'mkdir -p "$CLAUDE_CONFIG_DIR" '
            '"$CLAUDE_CONFIG_DIR/debug" '
            '"$CLAUDE_CONFIG_DIR/projects/-app" '
            '"$CLAUDE_CONFIG_DIR/shell-snapshots" '
            '"$CLAUDE_CONFIG_DIR/statsig" '
            '"$CLAUDE_CONFIG_DIR/todos" '
            '"$CLAUDE_CONFIG_DIR/skills"; '
            f"{proxy_prefix}"
            f"cd {shlex.quote(self.workspace_path)}; "
            f"{flags} < {quoted_prompt}"
        )

    def _render_proxy_prefix(self, remote_dir: str) -> str:
        if not self._uses_request_proxy():
            return ""

        ready_file = shlex.quote(f"{remote_dir}/request_proxy.env")
        log_file = shlex.quote(f"{remote_dir}/request_proxy.log")
        requests_log_file = shlex.quote(f"{remote_dir}/request_proxy_requests.jsonl")
        proxy_script = shlex.quote(CLAUDE_CODE_PROXY_REMOTE)
        dashscope_wait_timeout_arg = ""
        if self.dashscope_wait_timeout_sec is not None and self.dashscope_wait_timeout_sec > 0:
            dashscope_wait_timeout_arg = (
                "--dashscope-wait-timeout "
                f"{shlex.quote(str(self.dashscope_wait_timeout_sec))} "
            )
        return (
            'proxy_ready=' + ready_file + '; '
            'proxy_log=' + log_file + '; '
            'proxy_requests_log=' + requests_log_file + '; '
            'rm -f "$proxy_ready"; '
            'python_bin="$(command -v python3 || command -v python || true)"; '
            'if [ -z "$python_bin" ]; then echo "python3/python is required for Claude Code request proxy" >&2; exit 127; fi; '
            '"$python_bin" '
            f"{proxy_script} "
            '--upstream "$ANTHROPIC_BASE_URL" '
            '--listen-host 127.0.0.1 '
            '--port 0 '
            '--ready-file "$proxy_ready" '
            '--log-file "$proxy_log" '
            '--requests-log-file "$proxy_requests_log" '
            '--overrides "$CUA_HARNESS_CLAUDECODE_REQUEST_OVERRIDES" '
            f"{dashscope_wait_timeout_arg}"
            '& proxy_pid=$!; '
            'trap \'kill "$proxy_pid" 2>/dev/null || true\' EXIT INT TERM; '
            'i=0; while [ "$i" -lt 200 ] && [ ! -s "$proxy_ready" ]; do i=$((i + 1)); sleep 0.05; done; '
            'if [ ! -s "$proxy_ready" ]; then echo "Claude Code request proxy did not start" >&2; cat "$proxy_log" >&2 2>/dev/null || true; exit 127; fi; '
            '. "$proxy_ready"; '
        )

    async def _persist_proxy_logs(self, remote_dir: str, ep_dir: Path) -> dict[str, str]:
        if not self._uses_request_proxy():
            return {}

        files = [
            (
                "request_proxy.log",
                "request_proxy.log",
                "claude-code-proxy.log",
            ),
            (
                "request_proxy_requests.jsonl",
                "request_proxy_requests.jsonl",
                "claude-code-proxy-requests.jsonl",
            ),
            (
                "request_proxy.env",
                "request_proxy.env",
                None,
            ),
        ]
        downloaded: dict[str, str] = {}
        for remote_name, local_name, aggregate_name in files:
            remote_path = f"{remote_dir}/{remote_name}"
            local_path = ep_dir / local_name
            exists = await self.harbor_environment.exec(
                command=f"test -f {shlex.quote(remote_path)}",
                cwd="/",
                timeout_sec=10,
            )
            if exists.return_code != 0:
                continue
            try:
                await self.environment_adapter.download(remote_path, str(local_path))
            except Exception as exc:
                self._event(
                    "proxy_log_download_failed",
                    {
                        "role": self.role,
                        "episode": self.episode_index,
                        "remote_path": remote_path,
                        "error": repr(exc),
                    },
                )
                continue
            downloaded[local_name] = str(local_path)
            if aggregate_name is not None:
                self._append_local_log(local_path, self.output_dir / aggregate_name)
                if self.output_dir.parent != self.output_dir:
                    self._append_local_log(local_path, self.output_dir.parent / aggregate_name)

        if downloaded:
            self._event(
                "proxy_logs_downloaded",
                {
                    "role": self.role,
                    "episode": self.episode_index,
                    "logs": downloaded,
                },
            )
        return downloaded

    @staticmethod
    def _append_local_log(source: Path, target: Path) -> None:
        text = source.read_text(encoding="utf-8", errors="ignore")
        if not text:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", errors="ignore") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")

    def _command_env(self, remote_dir: str) -> dict[str, str]:
        env = {
            "ANTHROPIC_MODEL": self.model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": self.model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": self.model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": self.model,
            "CLAUDE_CODE_SUBAGENT_MODEL": self.model,
            "CLAUDE_CONFIG_DIR": f"{remote_dir}/claude_config",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            "DISABLE_PROMPT_CACHING": "1",
            "DISABLE_INTERLEAVED_THINKING": "1",
            "FORCE_AUTO_BACKGROUND_TASKS": "1",
            "ENABLE_BACKGROUND_TASKS": "1",
            "IS_SANDBOX": "1",
        }
        if self.api_key:
            env["ANTHROPIC_API_KEY"] = self.api_key
        if self.base_url:
            env["ANTHROPIC_BASE_URL"] = self.base_url
        if self.effort:
            env["CLAUDE_CODE_EFFORT_LEVEL"] = self.effort
        if self._uses_request_proxy():
            env["CUA_HARNESS_CLAUDECODE_REQUEST_PROXY"] = "1"
            env["CUA_HARNESS_CLAUDECODE_REQUEST_OVERRIDES"] = json.dumps(
                self.sampling_config,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        return env

    def _uses_request_proxy(self) -> bool:
        return bool(
            self.installed_request_proxy
            and self.sampling_config
            and _proxyable_anthropic_base_url(self.base_url)
        )

    def _effort_flag(self) -> str:
        if not self.effort:
            return ""
        return f"--effort {shlex.quote(self.effort)} "

    def _event(self, event: str, payload: dict[str, Any]) -> None:
        if self.event_logger is not None:
            self.event_logger(event, payload)

    def _record_usage(self, usage: dict[str, int]) -> None:
        for key in ("n_input_tokens", "n_output_tokens", "n_cache_tokens"):
            self.usage_totals[key] += int(usage.get(key) or 0)


def _anthropic_base_url(base_url: str | None) -> str | None:
    if not base_url:
        return None
    raw = base_url.rstrip("/")
    if raw.endswith("/v1"):
        raw = raw[:-3]
    return raw


def _proxyable_anthropic_base_url(base_url: str | None) -> bool:
    if not base_url:
        return False
    try:
        parsed = urlparse(base_url)
    except ValueError:
        return False

    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        return False
    return parsed.scheme in {"http", "https"}


def _is_command_timeout_exception(exc: Exception) -> bool:
    return bool(COMMAND_TIMEOUT_RE.search(str(exc)))


def _is_command_timeout_result(result: Any) -> bool:
    return_code = getattr(result, "return_code", None)
    if return_code not in {-1, 124, 137, 143}:
        return False
    text = "\n".join(
        part
        for part in (
            getattr(result, "stdout", "") or "",
            getattr(result, "stderr", "") or "",
        )
        if part
    )
    return bool(COMMAND_TIMEOUT_RE.search(text))


def _extract_claude_visible_output(stream_text: str) -> str:
    chunks: list[str] = []
    for line in stream_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        message = record.get("message") if isinstance(record.get("message"), dict) else record
        if not isinstance(message, dict):
            continue

        role = str(message.get("role") or record.get("role") or record.get("type") or "")
        if role not in {"assistant", "result"} and record.get("type") not in {"assistant", "result"}:
            continue

        if record.get("type") == "result":
            result_text = record.get("result")
            if isinstance(result_text, str) and result_text.strip():
                chunks.append(result_text.strip())
            continue

        text = _visible_text_from_content(message.get("content"))
        if text.strip():
            chunks.append(text.strip())
    return "\n\n".join(chunks)[-200_000:]


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
        if block_type in {
            "thinking",
            "reasoning",
            "redacted_thinking",
            "tool_use",
            "tool-use",
            "tool_result",
            "tool-result",
            "toolcall",
            "tool_call",
        }:
            continue
        text = block.get("text") or block.get("content") or block.get("output_text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts)


def _claude_steps_capped(stream_text: str, max_turns: int) -> bool:
    for line in reversed(stream_text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "result":
            continue
        subtype = record.get("subtype")
        turns = record.get("num_turns") or 0
        return bool(subtype == "error_max_turns" or (max_turns > 0 and turns >= max_turns))
    return False


def _usage_metadata(stream_text: str) -> dict[str, int]:
    result_usages: list[dict[str, Any]] = []
    fallback_usages: list[dict[str, Any]] = []
    for line in stream_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = record.get("usage")
        if not isinstance(usage, dict):
            message = record.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        if record.get("type") == "result":
            result_usages.append(usage)
        else:
            fallback_usages.append(usage)

    usages = result_usages or _dedupe_usage_records(fallback_usages)
    input_tokens = 0
    output_tokens = 0
    cache_tokens = 0
    for usage in usages:
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
        cache_tokens += cache_read + cache_creation
        input_tokens += int(usage.get("input_tokens") or 0) + cache_read + cache_creation
        output_tokens += int(usage.get("output_tokens") or 0)
    return {
        "n_input_tokens": input_tokens,
        "n_output_tokens": output_tokens,
        "n_cache_tokens": cache_tokens,
    }


def _dedupe_usage_records(usages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    previous: str | None = None
    for usage in usages:
        marker = json.dumps(usage, sort_keys=True, separators=(",", ":"), default=str)
        if marker == previous:
            continue
        deduped.append(usage)
        previous = marker
    return deduped
