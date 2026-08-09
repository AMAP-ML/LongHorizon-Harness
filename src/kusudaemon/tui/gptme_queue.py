"""Writes into a *running* gptme worker's external prompt queue.

gptme (github.com/gptme/gptme) ships its own durable mid-conversation
message queue: ``gptme/prompt_queue.py``'s ``queue_prompt(logdir, content)``
appends a JSON line to ``<logdir>/prompt-queue.jsonl``, and ``gptme.chat()``'s
own loop (``gptme/chat.py``'s ``_drain_external_prompt_queue``) already
polls that file between turns and injects each queued line as the next user
message. That mechanism, already shipped in gptme, is the entire "talk to a
running subagent mid-episode" feature — this module just needs to write the
same file in the same shape, from the long-lived TUI process, without
importing gptme itself (only ``adapters/_gptme_worker.py`` does that; see
its module docstring and ``pyproject.toml``'s ``tui``/``gptme`` extras).

The record shape (``{"content": str, "queued_at": iso8601}``) and the
``fcntl.flock``-guarded append are copied from gptme's own
``prompt_queue.py`` so a concurrent read by the worker's ``chat()`` call
never sees a half-written line.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    _fcntl: Any = importlib.import_module("fcntl")
except ImportError:  # pragma: no cover -- non-POSIX platforms
    _fcntl = None

QUEUE_FILENAME = "prompt-queue.jsonl"
_LOCK_FILENAME = ".prompt-queue.lock"


def queue_prompt(logdir: str | Path, content: str) -> None:
    """Append one operator message to a running gptme session's prompt
    queue. Safe to call at any point after the session's logdir has been
    discovered (see ``tui/state.py``'s ``node_gptme_logdir``) — if the
    worker process has already exited, the file is simply never drained,
    same as queuing to any conversation that isn't actively running."""
    logdir = Path(logdir)
    logdir.mkdir(parents=True, exist_ok=True)
    record = {
        "content": content,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    lock_path = logdir / _LOCK_FILENAME
    lock_path.touch(exist_ok=True)
    queue_path = logdir / QUEUE_FILENAME
    with lock_path.open("r+") as lock_fd:
        if _fcntl is not None:
            _fcntl.flock(lock_fd, _fcntl.LOCK_EX)
        try:
            with queue_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        finally:
            if _fcntl is not None:
                _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
