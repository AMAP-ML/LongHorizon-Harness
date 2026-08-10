"""Harbor entry points for running CUA-Harness with Claude Code role episodes."""
from __future__ import annotations

import json
import os
import posixpath
import shlex
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from harbor.agents.base import BaseAgent
from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from harbor_backends import (
    CLAUDE_VARIANT_NAME,
    CLAUDE_CODE_PROXY_HOST,
    CLAUDE_CODE_PROXY_REMOTE,
    HarborClaudeCodeEpisodeAdapter,
    HarborEnvironmentAdapter,
    ORCHESTRATOR_DENYLIST,
    ROLE_BACKEND_CLAUDE_CODE,
    TASK_DENYLIST,
    VERIFIER_DENYLIST,
    json_safe,
    safe_base_url,
)
from harness_types import EpisodeBudget, HarnessConfig
from orchestrator import run as run_cua_harness


_ROLE_SPECS = {
    "orchestrator": {
        "uses_verifier_model": False,
        "disallowed_tools": ORCHESTRATOR_DENYLIST,
    },
    "cli_task": {
        "uses_verifier_model": False,
        "disallowed_tools": TASK_DENYLIST,
    },
    "cli_verifier": {
        "uses_verifier_model": True,
        "disallowed_tools": VERIFIER_DENYLIST,
    },
}


class CuaHarnessClaudeCodeAgent(BaseAgent):
    """Harbor ``BaseAgent`` entry point for CUA-Harness.

    The class name is kept for backward compatibility with existing Harbor
    configs. Role episodes run through Claude Code.
    """

    SUPPORTS_ATIF = False
    SUPPORTS_WINDOWS = False

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        api_base: str | None = None,
        litellm_api_key: str | None = None,
        litellm_base_url: str | None = None,
        harness_model: str | None = None,
        agent_model: str | None = None,
        verifier_model: str | None = None,
        workspace_path: str | None = None,
        harness_dir: str | None = None,
        max_rounds: int | str | None = None,
        episode_timeout_sec: int | str | None = None,
        cli_task_timeout_sec: int | str | None = None,
        orchestrator_timeout_sec: int | str | None = None,
        verifier_timeout_sec: int | str | None = None,
        max_turns: int | str | None = None,
        orchestrator_max_turns: int | str | None = None,
        verifier_max_turns: int | str | None = None,
        verifier_context_chars: int | str | None = None,
        verifier_task_output_chars: int | str | None = None,
        verifier_prompt_chars: int | str | None = None,
        role_verified_context_chars: int | str | None = None,
        role_history_chars: int | str | None = None,
        role_memory_chars: int | str | None = None,
        effort: str | None = None,
        temperature: float | str | None = None,
        top_p: float | str | None = None,
        top_k: int | str | None = None,
        max_tokens: int | str | None = None,
        model_info: dict[str, Any] | None = None,
        llm_call_kwargs: dict[str, Any] | None = None,
        dashscope_wait_timeout_sec: int | str | None = None,
        install_claude: bool | str = True,
        claude_version: str | None = None,
        extra_env: dict[str, str] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        api_key = api_key or litellm_api_key or kwargs.pop("anthropic_api_key", None)
        base_url = base_url or api_base or litellm_base_url or kwargs.pop("anthropic_base_url", None)

        episode_timeout_sec = _first_non_none(
            episode_timeout_sec,
            kwargs.pop("task_timeout_sec", None),
            kwargs.pop("timeout", None),
        )
        cli_task_timeout_sec = _first_non_none(
            cli_task_timeout_sec,
            kwargs.pop("cli_timeout_sec", None),
        )
        max_turns = _first_non_none(max_turns, kwargs.pop("max_steps", None))

        super().__init__(
            logs_dir=logs_dir,
            model_name=model_name,
            *args,
            extra_env=extra_env,
            **kwargs,
        )

        default_config = HarnessConfig()
        self._api_key = api_key
        self._base_url = base_url
        self._harness_model = _coerce_str(
            harness_model or agent_model,
            env_keys=("CUA_HARNESS_MODEL",),
            env=self.extra_env,
        )
        self._verifier_model = _coerce_str(
            verifier_model,
            env_keys=("CUA_HARNESS_VERIFIER_MODEL",),
            env=self.extra_env,
        )
        self._workspace_path = workspace_path
        self._harness_dir = _coerce_str(
            harness_dir,
            env_keys=("CUA_HARNESS_DIR",),
            env=self.extra_env,
        )
        self._max_rounds = _coerce_int(
            max_rounds,
            env_keys=("CUA_HARNESS_MAX_ROUNDS", "CUA_HARNESS_MAX_EPISODES"),
            env=self.extra_env,
            default=30,
        )
        self._episode_timeout_sec = _coerce_int(
            episode_timeout_sec,
            env_keys=("CUA_HARNESS_TASK_TIMEOUT_SECONDS", "CUA_HARNESS_EPISODE_TIMEOUT_SECONDS"),
            env=self.extra_env,
            default=4200,
        )
        self._cli_task_timeout_sec = _coerce_int(
            cli_task_timeout_sec,
            env_keys=("CUA_HARNESS_CLI_TASK_TIMEOUT_SECONDS",),
            env=self.extra_env,
            default=1800,
        )
        self._orchestrator_timeout_sec = _coerce_int(
            orchestrator_timeout_sec,
            env_keys=("CUA_HARNESS_ORCHESTRATOR_TIMEOUT_SECONDS",),
            env=self.extra_env,
            default=min(self._episode_timeout_sec, 300),
        )
        self._verifier_timeout_sec = _coerce_int(
            verifier_timeout_sec,
            env_keys=("CUA_HARNESS_VERIFIER_TIMEOUT_SECONDS",),
            env=self.extra_env,
            default=min(self._episode_timeout_sec, 300),
        )
        self._max_turns = _coerce_int(
            max_turns,
            env_keys=("CUA_HARNESS_ROLE_MAX_TURNS", "CLAUDE_CODE_MAX_TURNS"),
            env=self.extra_env,
            default=20,
        )
        self._orchestrator_max_turns = _coerce_int(
            orchestrator_max_turns,
            env_keys=("CUA_HARNESS_ORCHESTRATOR_MAX_TURNS",),
            env=self.extra_env,
            default=min(self._max_turns, 8),
        )
        self._verifier_max_turns = _coerce_int(
            verifier_max_turns,
            env_keys=("CUA_HARNESS_VERIFIER_MAX_TURNS",),
            env=self.extra_env,
            default=min(self._max_turns, 8),
        )
        self._verifier_context_chars = max(
            0,
            _coerce_int(
                verifier_context_chars,
                env_keys=("CUA_HARNESS_VERIFIER_CONTEXT_CHARS",),
                env=self.extra_env,
                default=default_config.verifier_context_chars,
            ),
        )
        self._verifier_task_output_chars = max(
            0,
            _coerce_int(
                verifier_task_output_chars,
                env_keys=(
                    "CUA_HARNESS_VERIFIER_TASK_OUTPUT_CHARS",
                    "CUA_HARNESS_VERIFIER_TRAJECTORY_CHARS",
                ),
                env=self.extra_env,
                default=default_config.verifier_task_output_chars,
            ),
        )
        self._verifier_prompt_chars = max(
            10_000,
            _coerce_int(
                verifier_prompt_chars,
                env_keys=("CUA_HARNESS_VERIFIER_PROMPT_CHARS",),
                env=self.extra_env,
                default=default_config.verifier_prompt_chars,
            ),
        )
        self._role_verified_context_chars = max(
            0,
            _coerce_int(
                role_verified_context_chars,
                env_keys=("CUA_HARNESS_ROLE_VERIFIED_CONTEXT_CHARS",),
                env=self.extra_env,
                default=default_config.role_verified_context_chars,
            ),
        )
        self._role_history_chars = max(
            0,
            _coerce_int(
                role_history_chars,
                env_keys=("CUA_HARNESS_ROLE_HISTORY_CHARS",),
                env=self.extra_env,
                default=default_config.role_history_chars,
            ),
        )
        self._role_memory_chars = max(
            0,
            _coerce_int(
                role_memory_chars,
                env_keys=("CUA_HARNESS_ROLE_MEMORY_CHARS",),
                env=self.extra_env,
                default=default_config.role_memory_chars,
            ),
        )
        self._effort = _coerce_str(
            effort,
            env_keys=("CUA_HARNESS_CLAUDE_CODE_EFFORT", "CLAUDE_CODE_EFFORT_LEVEL"),
            env=self.extra_env,
        )
        self._model_info = dict(model_info or {}) if model_info is not None else None
        self._claude_sampling_config = _build_claude_sampling_config(
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_tokens=max_tokens,
            llm_call_kwargs=llm_call_kwargs,
        )
        self._dashscope_wait_timeout_sec = _coerce_optional_int(
            _first_non_none(
                dashscope_wait_timeout_sec,
                self.extra_env.get("CUA_HARNESS_DASHSCOPE_WAIT_TIMEOUT_SECONDS"),
                os.environ.get("CUA_HARNESS_DASHSCOPE_WAIT_TIMEOUT_SECONDS"),
            )
        )
        self._install_claude = _coerce_bool(install_claude)
        self._claude_version = _coerce_str(
            claude_version,
            env_keys=("CUA_HARNESS_CLAUDE_CODE_VERSION", "CLAUDE_CODE_VERSION"),
            env=self.extra_env,
        )
        self._installed_claude_code_proxy = False

    @staticmethod
    def name() -> str:
        return "cua-harness-claudecode"

    def version(self) -> str | None:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        if self._install_claude:
            installer = ClaudeCode(
                logs_dir=self.logs_dir / "claude_code_install",
                model_name=self.model_name,
                version=self._claude_version,
                extra_env=self.extra_env,
            )
            await installer.setup(environment)

        base_url = self._resolve_base_url()
        needs_request_proxy = (
            base_url
            and bool(self._claude_sampling_config)
            and _proxyable_anthropic_base_url(base_url)
            and CLAUDE_CODE_PROXY_HOST.is_file()
        )
        if needs_request_proxy:
            await self._ensure_request_proxy_python(environment, label="Claude Code")
            await environment.exec(
                command=f"mkdir -p {shlex.quote(posixpath.dirname(CLAUDE_CODE_PROXY_REMOTE))}",
                user="root",
                timeout_sec=30,
            )
            await environment.upload_file(CLAUDE_CODE_PROXY_HOST, CLAUDE_CODE_PROXY_REMOTE)
            await environment.exec(
                command=f"chmod 0755 {shlex.quote(CLAUDE_CODE_PROXY_REMOTE)}",
                user="root",
                timeout_sec=30,
            )
            self._installed_claude_code_proxy = True

    async def _ensure_request_proxy_python(
        self,
        environment: BaseEnvironment,
        *,
        label: str,
    ) -> None:
        command = (
            "set -e; "
            "if command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1; then exit 0; fi; "
            "if command -v apk >/dev/null 2>&1; then "
            "  apk add --no-cache python3; "
            "elif command -v apt-get >/dev/null 2>&1; then "
            "  apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y python3; "
            "elif command -v microdnf >/dev/null 2>&1; then "
            "  microdnf install -y python3; "
            "elif command -v dnf >/dev/null 2>&1; then "
            "  dnf install -y python3; "
            "elif command -v yum >/dev/null 2>&1; then "
            "  yum install -y python3; "
            "else "
            f"  echo 'Unable to install python3 for {label} request proxy' >&2; exit 127; "
            "fi; "
            "command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1"
        )
        result = await environment.exec(
            command=command,
            user="root",
            timeout_sec=300,
        )
        if result.return_code != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                f"Failed to install python3 for {label} request proxy"
                + (f": {detail[-2000:]}" if detail else "")
            )

    async def _ensure_claude_request_proxy_python(self, environment: BaseEnvironment) -> None:
        await self._ensure_request_proxy_python(environment, label="Claude Code")

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        variant_name = self._variant_name()
        output_dir = self.logs_dir / variant_name
        output_dir.mkdir(parents=True, exist_ok=True)
        events_path = output_dir / "events.jsonl"

        model = self._resolve_model()
        verifier_model = self._resolve_verifier_model(model)
        api_key = self._resolve_api_key()
        base_url = self._resolve_base_url()
        role_backend = self._role_backend_name()

        def log_event(event: str, payload: dict[str, Any]) -> None:
            record = {"ts": time.time(), "event": event, **json_safe(payload)}
            with events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

        workspace_path, workspace_resolution = await self._resolve_workspace_path(environment)
        log_event("workspace_path_resolved", workspace_resolution)
        env_adapter = HarborEnvironmentAdapter(environment, workspace_path=workspace_path)

        start_metadata = {
            "model": model,
            "verifier_model": verifier_model,
            "role_backend": role_backend,
            "base_url": safe_base_url(base_url),
            "workspace_path": workspace_path,
            "max_rounds": self._max_rounds,
            "episode_timeout_sec": self._episode_timeout_sec,
            "cli_task_timeout_sec": self._cli_task_timeout_sec,
            "orchestrator_timeout_sec": self._orchestrator_timeout_sec,
            "verifier_timeout_sec": self._verifier_timeout_sec,
            "max_turns": self._max_turns,
            "orchestrator_max_turns": self._orchestrator_max_turns,
            "verifier_max_turns": self._verifier_max_turns,
            "verifier_context_chars": self._verifier_context_chars,
            "verifier_task_output_chars": self._verifier_task_output_chars,
            "verifier_prompt_chars": self._verifier_prompt_chars,
            "role_verified_context_chars": self._role_verified_context_chars,
            "role_history_chars": self._role_history_chars,
            "role_memory_chars": self._role_memory_chars,
            "model_info": self._model_info,
        }
        start_metadata.update(self._backend_start_metadata())
        log_event("harbor_cua_harness_start", start_metadata)

        role_agents = self._make_role_agents(
            environment=environment,
            env_adapter=env_adapter,
            output_dir=output_dir,
            model=model,
            verifier_model=verifier_model,
            api_key=api_key,
            base_url=base_url,
            workspace_path=workspace_path,
            event_logger=log_event,
        )
        config = self._build_harness_config(
            output_dir=output_dir,
            workspace_path=workspace_path,
            model=model,
            verifier_model=verifier_model,
            api_key=api_key,
            base_url=base_url,
        )

        started = time.monotonic()
        report = await run_cua_harness(
            task=instruction,
            env=env_adapter,
            agent=role_agents["cli_task"],
            orchestrator_agent=role_agents["orchestrator"],
            cli_task_agent=role_agents["cli_task"],
            cli_verifier_agent=role_agents["cli_verifier"],
            config=config,
        )
        elapsed = time.monotonic() - started

        usage_totals = _role_usage_totals(role_agents)
        if usage_totals is not None:
            context.n_input_tokens = usage_totals["n_input_tokens"]
            context.n_output_tokens = usage_totals["n_output_tokens"]
            context.n_cache_tokens = usage_totals["n_cache_tokens"]

        report_path = output_dir / "cua_harness" / "report.json"
        if not report_path.exists():
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        context.metadata = {
            "variant": variant_name,
            "model": model,
            "verifier_model": verifier_model,
            "role_backend": role_backend,
            "workspace_path": workspace_path,
            "elapsed_seconds": round(elapsed, 3),
            "report_path": str(report_path),
            "completion_satisfied": bool(report.get("completion_satisfied")),
            "status": report.get("status"),
            "rounds_run": report.get("rounds_run"),
            "abort_reason": report.get("abort_reason"),
            "model_info": self._model_info,
            "usage_totals": usage_totals,
            "report": report,
        }
        context.metadata.update(self._backend_context_metadata())
        log_event("harbor_cua_harness_done", context.metadata)

    def _make_role_agents(
        self,
        *,
        environment: BaseEnvironment,
        env_adapter: HarborEnvironmentAdapter,
        output_dir: Path,
        model: str,
        verifier_model: str,
        api_key: str | None,
        base_url: str | None,
        workspace_path: str,
        event_logger: Any,
    ) -> dict[str, Any]:
        role_agents: dict[str, Any] = {}
        for role, spec in _ROLE_SPECS.items():
            role_model = verifier_model if spec["uses_verifier_model"] else model
            role_agents[role] = self._make_episode_adapter(
                environment=environment,
                env_adapter=env_adapter,
                output_dir=output_dir,
                role=role,
                model=role_model,
                api_key=api_key,
                base_url=base_url,
                workspace_path=workspace_path,
                disallowed_tools=spec["disallowed_tools"],
                event_logger=event_logger,
            )
        return role_agents

    def _make_episode_adapter(
        self,
        *,
        environment: BaseEnvironment,
        env_adapter: HarborEnvironmentAdapter,
        output_dir: Path,
        role: str,
        model: str,
        api_key: str | None,
        base_url: str | None,
        workspace_path: str,
        disallowed_tools: str,
        event_logger: Any,
    ) -> Any:
        return HarborClaudeCodeEpisodeAdapter(
            harbor_environment=environment,
            environment_adapter=env_adapter,
            output_dir=output_dir,
            role=role,
            model=model,
            api_key=api_key,
            base_url=base_url,
            effort=self._effort,
            workspace_path=workspace_path,
            disallowed_tools=disallowed_tools,
            installed_request_proxy=self._installed_claude_code_proxy,
            sampling_config=self._claude_sampling_config,
            dashscope_wait_timeout_sec=self._dashscope_wait_timeout_sec,
            event_logger=event_logger,
        )

    def _build_harness_config(
        self,
        *,
        output_dir: Path,
        workspace_path: str,
        model: str,
        verifier_model: str,
        api_key: str | None,
        base_url: str | None,
    ) -> HarnessConfig:
        return HarnessConfig(
            max_total_episodes=self._max_rounds,
            episode_budget=EpisodeBudget(
                max_turns=self._max_turns,
                max_duration_seconds=self._episode_timeout_sec,
            ),
            orchestrator_budget=EpisodeBudget(
                max_turns=self._orchestrator_max_turns,
                max_duration_seconds=self._orchestrator_timeout_sec,
            ),
            cli_task_budget=EpisodeBudget(
                max_turns=self._max_turns,
                max_duration_seconds=self._cli_task_timeout_sec,
            ),
            verifier_budget=EpisodeBudget(
                max_turns=self._verifier_max_turns,
                max_duration_seconds=self._verifier_timeout_sec,
            ),
            role_verifier_budget=EpisodeBudget(
                max_turns=self._verifier_max_turns,
                max_duration_seconds=self._verifier_timeout_sec,
            ),
            workspace_path=workspace_path,
            harness_dir=self._configured_harness_dir(workspace_path),
            log_dir=str(output_dir / "cua_harness"),
            verifier_model=verifier_model,
            agent_model=model,
            verifier_context_chars=self._verifier_context_chars,
            verifier_task_output_chars=self._verifier_task_output_chars,
            verifier_prompt_chars=self._verifier_prompt_chars,
            role_verified_context_chars=self._role_verified_context_chars,
            role_history_chars=self._role_history_chars,
            role_memory_chars=self._role_memory_chars,
            api_key=api_key,
            base_url=base_url,
        )

    def _resolve_api_key(self) -> str | None:
        return (
            self._api_key
            or self.extra_env.get("ANTHROPIC_API_KEY")
            or self.extra_env.get("ANTHROPIC_AUTH_TOKEN")
            or self.extra_env.get("OPENAI_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("OPENAI_API_KEY")
        )

    def _resolve_base_url(self) -> str | None:
        return (
            self._base_url
            or self.extra_env.get("ANTHROPIC_BASE_URL")
            or self.extra_env.get("OPENAI_BASE_URL")
            or os.environ.get("ANTHROPIC_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
        )

    def _resolve_model(self) -> str:
        return (
            self._harness_model
            or self.model_name
            or self.extra_env.get("CUA_HARNESS_MODEL")
            or self.extra_env.get("ANTHROPIC_MODEL")
            or os.environ.get("CUA_HARNESS_MODEL")
            or os.environ.get("ANTHROPIC_MODEL")
            or "claude-sonnet-4-20250514"
        )

    def _resolve_verifier_model(self, default_model: str) -> str:
        return self._verifier_model or default_model

    async def _resolve_workspace_path(
        self,
        environment: BaseEnvironment,
    ) -> tuple[str, dict[str, Any]]:
        configured = self._configured_workspace_path(environment)
        detected_cwd = None if configured else await _container_current_directory(environment)
        candidates = _workspace_candidates(configured or detected_cwd)
        checked: list[dict[str, Any]] = []

        for candidate in candidates:
            exists = await _container_directory_exists(environment, candidate)
            checked.append({"path": candidate, "exists": exists})
            if exists:
                return (
                    candidate,
                    {
                        "requested": configured,
                        "detected_cwd": detected_cwd,
                        "resolved": candidate,
                        "reason": (
                            "requested_exists"
                            if configured and candidate == configured
                            else "detected_cwd_exists"
                            if detected_cwd and candidate == detected_cwd
                            else "fallback_exists"
                        ),
                        "checked": checked,
                    },
                )

        raise RuntimeError(
            "Unable to resolve a valid workspace directory in the task container. "
            f"Checked: {', '.join(candidates)}"
        )

    def _configured_workspace_path(self, environment: BaseEnvironment) -> str | None:
        if self._workspace_path:
            return self._workspace_path.rstrip("/") or "/app"
        task_env_config = getattr(environment, "task_env_config", None)
        workdir = getattr(task_env_config, "workdir", None)
        if isinstance(workdir, str) and workdir.strip():
            return workdir.rstrip("/") or "/app"
        return None

    def _configured_harness_dir(self, workspace_path: str) -> str:
        if self._harness_dir:
            return self._harness_dir.rstrip("/") or "/"
        return f"{workspace_path.rstrip('/')}/.cua_harness"

    def _variant_name(self) -> str:
        return CLAUDE_VARIANT_NAME

    def _role_backend_name(self) -> str:
        return ROLE_BACKEND_CLAUDE_CODE

    def _backend_start_metadata(self) -> dict[str, Any]:
        return {"claude_sampling_config": self._claude_sampling_config}

    def _backend_context_metadata(self) -> dict[str, Any]:
        return {"claude_sampling_config": self._claude_sampling_config}




HarborCuaHarnessClaudeCodeAgent = CuaHarnessClaudeCodeAgent


def _workspace_candidates(configured: str | None) -> list[str]:
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    candidates.extend(("/app", "/workspace", "/workdir", "/repo"))

    seen: set[str] = set()
    deduped: list[str] = []
    for candidate in candidates:
        normalized = candidate.rstrip("/") or "/"
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


async def _container_directory_exists(
    environment: BaseEnvironment,
    path: str,
) -> bool:
    result = await environment.exec(
        command=f"test -d {shlex.quote(path)}",
        cwd="/",
        timeout_sec=10,
    )
    return int(result.return_code) == 0


async def _container_current_directory(environment: BaseEnvironment) -> str | None:
    result = await environment.exec(
        command="pwd",
        cwd=None,
        timeout_sec=10,
    )
    if int(result.return_code) != 0:
        return None
    path = (result.stdout or "").strip().splitlines()[-1:]
    if not path:
        return None
    value = path[0].strip()
    if not value.startswith("/") or value == "/":
        return None
    return value.rstrip("/")


def _build_claude_sampling_config(
    *,
    temperature: float | str | None,
    top_p: float | str | None,
    top_k: int | str | None,
    max_tokens: int | str | None,
    llm_call_kwargs: dict[str, Any] | None,
) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    call_kwargs = dict(llm_call_kwargs or {})

    for key in ("temperature", "top_p", "top_k", "max_tokens"):
        if key in call_kwargs:
            _set_sampling_value(resolved, key, call_kwargs[key])

    extra_body = call_kwargs.get("extra_body")
    if isinstance(extra_body, dict):
        for key, value in extra_body.items():
            _set_sampling_value(resolved, str(key), value)

    _set_sampling_value(resolved, "temperature", temperature)
    _set_sampling_value(resolved, "top_p", top_p)
    _set_sampling_value(resolved, "top_k", top_k)
    _set_sampling_value(resolved, "max_tokens", max_tokens)
    return resolved


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


def _set_sampling_value(target: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    if key in {"temperature", "top_p"}:
        coerced_float = _coerce_float(value)
        if coerced_float is not None:
            target[key] = coerced_float
        return
    if key in {"top_k", "max_tokens"}:
        coerced_int = _coerce_optional_int(value)
        if coerced_int is not None:
            target[key] = coerced_int
        return
    target[key] = json_safe(value)






def _role_usage_totals(role_agents: dict[str, Any]) -> dict[str, int] | None:
    totals = {
        "n_input_tokens": 0,
        "n_output_tokens": 0,
        "n_cache_tokens": 0,
    }
    saw_usage = False
    for agent in role_agents.values():
        usage = getattr(agent, "usage_totals", None)
        if not isinstance(usage, dict):
            continue
        role_has_usage = False
        for key in totals:
            value = usage.get(key)
            if value is None:
                continue
            totals[key] += int(value or 0)
            role_has_usage = True
        saw_usage = saw_usage or role_has_usage
    return totals if saw_usage else None


def _first_non_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _coerce_optional_int(value: int | str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: float | str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(
    value: int | str | None,
    *,
    env_keys: tuple[str, ...],
    env: dict[str, str] | None = None,
    default: int,
) -> int:
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if env:
        for key in env_keys:
            raw = env.get(key)
            if raw:
                try:
                    return int(raw)
                except ValueError:
                    pass
    for key in env_keys:
        raw = os.environ.get(key)
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
    return default


def _coerce_str(
    value: str | None,
    *,
    env_keys: tuple[str, ...],
    env: dict[str, str] | None = None,
) -> str | None:
    if value is not None and str(value).strip():
        return str(value).strip()
    if env:
        for key in env_keys:
            raw = env.get(key)
            if raw is not None and str(raw).strip():
                return str(raw).strip()
    for key in env_keys:
        raw = os.environ.get(key)
        if raw is not None and raw.strip():
            return raw.strip()
    return None


def _coerce_bool(value: bool | str) -> bool:
    if isinstance(value, bool):
        return value
    return value.strip().lower() not in {"0", "false", "no", "off"}
