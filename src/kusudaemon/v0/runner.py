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
from .run_dir import ensure_node_trace_path, events_path, node_artifact_path

_SESSION_POLL_INTERVAL_SECONDS = 0.05


# §E24 (2026-08-13): events that consume a node's episode_completed,
# invalidating the resume-after-complete replay. The replay exists for one
# window only: the episode ended but the harness never processed the result
# (kill -9 between the completion append and the tree save). Any of these
# events AFTER the completion means the harness (or the operator) already
# acted on it — the node was transitioned or deliberately reset — so a
# later dispatch must run a FRESH episode. Before this check every gate
# failure poisoned the node: retries and operator redispatches replayed the
# old completion instead of running an episode (observed live: a T1 writer
# that 429'd once then "failed" its remaining attempts in ~4 ms each).
_REPLAY_INVALIDATING_TYPES = frozenset(
    {
        "node_gate_failed",           # round loop transitioned the node
        "node_review_failed",         # review consumed the completion
        "node_redispatch_requested",  # operator redispatch — new attempt series
        "node_reopened",              # operator direct reset — new attempt series
    }
)


def _completion_consumed(
    events: list[dict[str, Any]], node_id: str, completed: dict[str, Any]
) -> bool:
    base = completed.get("ts") or 0
    for event in events:
        if event.get("node_id") != node_id:
            continue
        if event.get("type") in _REPLAY_INVALIDATING_TYPES and (event.get("ts") or 0) > base:
            return True
    return False


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

    # One parse of events.jsonl for the whole dispatch; every per-node query
    # scans the parsed list in memory. events.jsonl is append-only and
    # fsync'd per record (EventLog's contract), so a single read sees a
    # consistent prefix of the durable log.
    events = log.read_all()
    completed = EventLog.scan(events, node_id, "episode_completed")
    if completed is not None and not _completion_consumed(events, node_id, completed):
        # Resume-after-complete is a pure no-op: replay the recorded result
        # instead of re-dispatching, so calling run_node twice never produces
        # two artifacts or two terminal events for the same node. §E24: only
        # in the crash window — a completion that a gate failure, review
        # failure, or operator reset already consumed must not be replayed
        # (a retry or redispatch is a new attempt and needs a real episode).
        return _result_from_completed_event(completed)

    dispatched = EventLog.scan(events, node_id, "node_dispatched")
    session = EventLog.scan(events, node_id, "session_captured")
    supports_resume = bool(getattr(adapter, "supports_session_resume", False))

    resume_session_id: str | None = None
    if session is not None and session.get("session_id"):
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
        # so redispatch fresh. "The original prompt" is the caller's
        # framing, not this module's: run_node has no memory of what it was
        # given before and just dispatches whatever prompt it's handed this
        # call — a caller may legitimately supply a different one on a
        # redispatch (PLAN-zeromem.md §9: a retry's prompt carries the prior
        # attempt's located defect forward, so it differs from the first).
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

    trace_path = ensure_node_trace_path(run_dir, node_id)
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
    #
    # PLAN-AUDIT.md §E13: this watcher exists to power session-resume
    # bookkeeping, and only an adapter with supports_session_resume=True can
    # ever act on the session it captures (see the resume_session_id branch
    # above). The only real Writer backend, gptme, has this False — so for
    # it the poll loop below used to run for the entire episode duration,
    # every ~50ms, finding nothing, for no functional benefit: no code path
    # reads a gptme session_captured event, and the dashboard's own
    # `_last_logdir` re-derives the logdir straight off the trace file
    # itself rather than depending on this event. Skip starting the task
    # entirely rather than starting it and having it find nothing.
    stop_watching = asyncio.Event()
    watcher: asyncio.Task[None] | None = None
    if supports_resume:
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
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    artifact_path = node_artifact_path(run_dir, node_id)
    # gptme writes the real artifact itself, mid-episode, via its own
    # save/patch tool calls against this exact path — now that the prompt
    # actually states that path (PLAN.md §D0; it didn't before) — so if the
    # file already has content, that *is* the artifact and must never be
    # clobbered by anything derived from the episode's raw output after the
    # fact. Only fall back when the agent never actually wrote anything
    # (crash, or an adapter with no file tools that relies on "last message
    # becomes the artifact").
    existing = artifact_path.read_text(encoding="utf-8") if artifact_path.exists() else ""
    if not existing.strip():
        if getattr(adapter, "has_file_tools", False):
            # PLAN.md §D0: an adapter that can write files (gptme) had every
            # opportunity to save the real artifact. An empty file here is a
            # genuine failure, not raw log noise to paper over — write "" so
            # the `nonempty` gate fails cleanly instead of a chat sentence
            # (or a stray save-fence) masquerading as a passed node.
            artifact_path.write_text("", encoding="utf-8")
        else:
            visible_output = result.metadata.get("assistant_visible_output") or ""
            # actions_log_diagnostics_only (cli_agent.py) is set when an
            # adapter *has* a structured visible-output parser (gptme's
            # --output-format json) but it found no real assistant message —
            # meaning actions_log is raw protocol/tool-call JSON, not prose,
            # and must not be written as if it were content (this is exactly
            # how a bare ``{"type": "logdir", ...}`` bootstrap line —
            # everything printed before a crash mid-episode — used to end up
            # masquerading as a node's artifact). An adapter with no parser
            # at all (fake/test adapters) still falls back to the raw log,
            # unchanged.
            diagnostics_only = bool(result.metadata.get("actions_log_diagnostics_only"))
            artifact_text = visible_output or ("" if diagnostics_only else result.actions_log)
            artifact_path.write_text(artifact_text, encoding="utf-8")

    # v0 does not write manifest.jsonl: a single Writer node has no gates to
    # evaluate and no way to derive the PLAN.md §6 manifest schema. That
    # line is written by the caller once gates have actually run — v1's
    # round loop (src/kusudaemon/v1/manifest.py) is the first caller that
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
    captured_logdirs: set[str] = set()
    while True:
        try:
            # Binary mode and byte offsets: text-mode seek only accepts
            # opaque cookies from tell(), and readlines()+tell() consumes a
            # partial trailing line — the offset advances past the bytes
            # that will be written next, so a session_id that lands in a
            # torn line is never re-read (§11.9). Advance only past the last
            # complete line instead.
            with open(trace_path, "rb") as fh:
                fh.seek(offset)
                data = fh.read()
        except OSError:
            data = b""
        last_newline = data.rfind(b"\n")
        if last_newline == -1:
            new_lines = []
        else:
            offset += last_newline + 1
            new_lines = data[: last_newline + 1].decode("utf-8", errors="replace").splitlines()
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
            # PLAN-AUDIT.md §E13: don't hardcode session_id as the only
            # event shape a resume-capable adapter can emit. A
            # `{"type": "logdir", ...}` line (the shape _gptme_worker.py
            # actually emits, though gptme itself has no session-resume
            # support today) is captured the same way so a future
            # resume-capable adapter whose continuity token is a logdir
            # rather than a session id doesn't need a second watcher.
            # Non-terminal update: keep polling for a real session_id.
            if record.get("type") == "logdir" and record.get("logdir"):
                ld = str(record.get("logdir"))
                if ld not in captured_logdirs:
                    captured_logdirs.add(ld)
                    log.append(
                        {
                            "node_id": node_id,
                            "role": "writer",
                            "round": 0,
                            "type": "session_captured",
                            "logdir": ld,
                        }
                    )
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
