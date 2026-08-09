"""The resumable single-node runner (PLAN.md §13 v0).

One idempotent entrypoint, ``run_node``, handles both first-run and resume:
calling it again after a `kill -9` just continues correctly from whatever
events are already durable on disk. There is no separate resume code path —
that convergence *is* the thing this module exists to prove.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from ..adapters.base import AgentAdapter
from ..environment.base import Environment
from ..types import EpisodeBudget, EpisodeResult
from .events import EventLog
from .run_dir import events_path, node_artifact_path, node_trace_path

_SESSION_POLL_INTERVAL_SECONDS = 0.05


async def run_node(
    run_dir: str | Path,
    node_id: str,
    prompt: str,
    adapter: AgentAdapter,
    env: Environment,
    budget: EpisodeBudget,
) -> EpisodeResult:
    run_dir = Path(run_dir)
    log = EventLog(events_path(run_dir))

    completed = log.last_event(node_id, "episode_completed")
    if completed is not None:
        # Resume-after-complete is a pure no-op: replay the recorded result
        # instead of re-dispatching, so calling run_node twice never produces
        # two artifacts or two terminal events for the same node.
        return _result_from_completed_event(completed)

    dispatched = log.last_event(node_id, "node_dispatched")
    session = log.last_event(node_id, "session_captured")
    supports_resume = bool(getattr(adapter, "supports_session_resume", False))

    resume_session_id: str | None = None
    if session is not None:
        if supports_resume:
            resume_session_id = session.get("session_id")
            log.append(
                {
                    "node_id": node_id,
                    "role": "writer",
                    "round": 0,
                    "type": "node_redispatched",
                    "reason": "resumed_session",
                }
            )
        else:
            # A session id was captured last time but this adapter (e.g.
            # Codex today) has no continuation mechanism. Falling back to a
            # fresh redispatch is the documented behavior rather than an
            # error — see ClaudeCodeAdapter.supports_session_resume.
            log.append(
                {
                    "node_id": node_id,
                    "role": "writer",
                    "round": 0,
                    "type": "node_redispatched",
                    "reason": "resume_unsupported",
                }
            )
    elif dispatched is not None:
        # Crashed before any output ever hit disk: nothing to continue from,
        # so redispatch fresh from the original prompt.
        log.append(
            {
                "node_id": node_id,
                "role": "writer",
                "round": 0,
                "type": "node_redispatched",
                "reason": "no_session_captured",
            }
        )
    else:
        log.append(
            {
                "node_id": node_id,
                "role": "writer",
                "round": 0,
                "type": "node_dispatched",
            }
        )

    trace_path = node_trace_path(run_dir, node_id)
    # A prior crashed attempt can leave stale content in trace_path. Clear it
    # before dispatching so the watcher below can't race the new subprocess
    # and mistake a leftover line from the old attempt for a fresh capture —
    # LocalEnvironment.exec truncates and recreates this file itself once the
    # new subprocess actually starts, but that happens a few awaits later.
    if trace_path.exists():
        trace_path.unlink()

    # LocalEnvironment.exec tees stdout to trace_path live, line by line, as
    # the subprocess runs — so this tail can see session_id the instant the
    # agent CLI emits it, without waiting for run_episode() to return. A
    # kill -9 can land any time after that line hits disk.
    stop_watching = asyncio.Event()
    watcher = asyncio.create_task(
        _watch_for_session_id(trace_path, log, node_id, stop_watching)
    )
    try:
        episode_kwargs: dict[str, Any] = {"live_trajectory_path": str(trace_path)}
        if resume_session_id is not None:
            episode_kwargs["resume_session_id"] = resume_session_id
        result = await adapter.run_episode(prompt, env, budget, **episode_kwargs)
    finally:
        stop_watching.set()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher

    artifact_path = node_artifact_path(run_dir, node_id)
    artifact_text = result.metadata.get("assistant_visible_output") or result.actions_log or ""
    artifact_path.write_text(artifact_text, encoding="utf-8")

    # v0 does not write manifest.jsonl: a single Writer node has no gates to
    # evaluate and no way to derive the PLAN.md §6 manifest schema. That
    # line is written by the caller once gates have actually run — v1's
    # round loop (src/waypoint/v1/manifest.py) is the first caller that
    # can do so correctly.
    log.append(
        {
            "node_id": node_id,
            "role": "writer",
            "round": 0,
            "type": "episode_completed",
            "status": result.status,
            "artifact_path": str(artifact_path),
            "error": result.error,
            "duration_ms": result.duration_ms,
        }
    )
    return result


async def run(
    run_dir: str | Path,
    node_id: str,
    prompt: str,
    adapter: AgentAdapter,
    env: Environment,
    budget: EpisodeBudget,
) -> EpisodeResult:
    """First-run-facing alias. Identical to ``resume`` — see module docstring."""
    return await run_node(run_dir, node_id, prompt, adapter, env, budget)


async def resume(
    run_dir: str | Path,
    node_id: str,
    prompt: str,
    adapter: AgentAdapter,
    env: Environment,
    budget: EpisodeBudget,
) -> EpisodeResult:
    """Resume-facing alias. Identical to ``run`` — see module docstring."""
    return await run_node(run_dir, node_id, prompt, adapter, env, budget)


async def _watch_for_session_id(
    trace_path: Path,
    log: EventLog,
    node_id: str,
    stop: asyncio.Event,
) -> None:
    while not trace_path.exists():
        if stop.is_set():
            return
        await asyncio.sleep(_SESSION_POLL_INTERVAL_SECONDS)

    offset = 0
    while True:
        try:
            with open(trace_path, "r", encoding="utf-8") as fh:
                fh.seek(offset)
                new_lines = fh.readlines()
                offset = fh.tell()
        except OSError:
            new_lines = []
        for line in new_lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = record.get("session_id")
            if session_id:
                log.append(
                    {
                        "node_id": node_id,
                        "role": "writer",
                        "round": 0,
                        "type": "session_captured",
                        "session_id": session_id,
                    }
                )
                return
        if stop.is_set():
            return
        await asyncio.sleep(_SESSION_POLL_INTERVAL_SECONDS)


def _result_from_completed_event(event: dict[str, Any]) -> EpisodeResult:
    status = event.get("status")
    if status not in ("done", "timeout", "error", "cancelled"):
        status = "done"
    return EpisodeResult(
        status=status,
        actions_log="",
        error=event.get("error"),
        duration_ms=int(event.get("duration_ms") or 0),
        metadata={
            "artifact_path": event.get("artifact_path"),
            "replayed_from_event_log": True,
        },
    )
