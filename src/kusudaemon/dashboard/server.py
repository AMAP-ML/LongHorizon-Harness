"""Stdlib HTTP server mounting :class:`RunState` (PLAN.md §11's "View
surface: local web app") — the ``kusudaemon`` control surface, replacing
the Textual TUI (``tui/app.py``, deleted 2026-08-09 the same day this was
rebuilt; see CLAUDE.md's v5 section for the full back-and-forth: web app
-> TUI -> web app again, this time carrying over every TUI-only feature —
Subagents view, live mid-episode interject, colored diff history, node
reopen — that the original dashboard never had).

No new dependency: routing, JSON, and static-file serving are all
``http.server``/``urllib``/``json`` from the standard library, matching
the rest of the harness's "stdlib + packaging/tomli" rule. One
``ThreadingHTTPServer`` so a long-lived SSE connection (``/api/stream``)
never blocks any other request.

This module owns *transport* only — request parsing, routing, JSON
encoding, path-traversal-safe static serving, and ``control_enabled``
gating for every mutating route. ``RunState`` (``state.py``) itself has no
opinion on read-only mode — that concept doesn't apply to a TUI or a bound
terminal, and it was folded back in here, one layer up, only because a web
server (unlike either of those) may be reachable from more than just the
operator's own machine. Every read or mutation past that gate is a single
call into ``RunState``, which is the authority on what's safe to do to a
run directory (id validation, path-traversal checks, etc).

Two ways to reach this server (PLAN.md §11: "a separate process watching
the run directory... can be attached from anywhere"):

* ``kusudaemon serve --runs-root <dir>`` — the primary, standalone view
  surface; a `run`/`resume`/`--detach` process needs nothing from this
  module to make progress, so the server can start, stop, or crash without
  ever touching a run.
* bare ``kusudaemon`` (no subcommand) — shorthand for ``serve`` with the
  default ``--runs-root``, matching the old bare-invocation ergonomics.

**PLAN.md §C4 — Auth and concurrency hardening (2026-08-11).**

The original dashboard shipped with no auth at all — ``control_enabled``
gates mutating actions, but any anonymous requester reaching the bound
host can read every artifact, every trace, every approval. Loopback-only
default binding made that a small exposure, but ``--host 0.0.0.0`` (the
explicit flag this server exposes) was the only thing keeping the
operator's run directory from being reachable from anywhere on the LAN.

Three hard-earned rules, all enforced in this module:

1. **Refuse to start on a non-loopback host without an auth token.** The
   ``--host`` flag explicitly invites off-loopback binding, so the same
   flag's safety check is what refuses it: ``_assert_safe_host`` raises
   before ``make_server`` ever binds the socket, with a message naming
   the ``--auth-token`` flag the operator needs to set. ``127.0.0.1`` and
   ``::1`` are loopback; any other non-empty host string needs a token.
2. **Token comparison uses ``hmac.compare_digest``** — never ``==``. A
   timing-attack-resistant comparison is cheap; the alternative is not.
3. **Cookie for SSE, not ``Authorization``.** ``EventSource`` cannot set
   custom headers, so the SSE endpoint cannot carry an ``Authorization``
   token the way every fetch route can. ``/api/attach`` issues a
   ``Set-Cookie`` when a token is configured; ``_serve_stream`` and every
   route then validate that cookie with the same timing-safe comparison.
   A non-loopback server without a token never starts (rule 1), so the
   cookie is the second factor for off-loopback and a no-op on loopback
   (where the cookie gate always passes when the token is empty).

Plus ``max_concurrent_runs`` (§C4: "max_concurrent_runs with a surfaced
429"): ``_post_runs`` returns 429 instead of starting a run when the
count of in-flight hosted runs reaches the cap. The cap defaults to 4
(one driver per run is the design; more than a handful is usually a
misconfiguration — provider rate limits will clamp it harder than the
dashboard would, and silently queuing is worse than surfacing the 429).
"""

from __future__ import annotations

import hmac
import http.cookies
import json
import mimetypes
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from . import rendering
from .state import RunState

_STATIC_DIR = Path(__file__).parent / "static"
_STREAM_INTERVAL = 1.5

# §PERF: the /api/node/<id>/thinking payload cap (see the route). The chat
# tab is polled every _STREAM_INTERVAL while an episode runs; a long
# episode's full trace is megabytes and grows every turn, so the server
# returns only the tail and reports the real total.
MAX_THINKING_ENTRIES = 1500

# §C4: the maximum number of concurrently-hosted runs the dashboard will
# accept. Each hosted run owns a driver thread with a process and a provider
# connection; more than a small number is almost always a misconfiguration
# (provider rate limits clamp harder than this cap), so surfacing a 429 is
# the right thing rather than silently queuing or thrashing.
DEFAULT_MAX_CONCURRENT_RUNS = 4

# Cookie name for the auth token set on /api/attach when one is configured.
# Marked HttpOnly + SameSite=Strict per the standard pattern — the dashboard
# client never reads it directly, the browser carries it on every same-origin
# request including the SSE EventSource.
_AUTH_COOKIE_NAME = "kusudaemon_auth"

_Handler = Callable[["DashboardRequestHandler", "re.Match[str]", dict[str, Any]], tuple[int, Any]]


def _route(method: str, pattern: str) -> Callable[[_Handler], _Handler]:
    def register(fn: _Handler) -> _Handler:
        _ROUTES.append((method, re.compile(pattern), fn))
        return fn

    return register


_ROUTES: list[tuple[str, "re.Pattern[str]", _Handler]] = []


@_route("GET", r"^/api/snapshot$")
def _get_snapshot(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    # control_enabled lives on the handler, not RunState (see module
    # docstring) -- stitched into the payload here so the frontend can grey
    # out mutating controls without a second round trip. Same for
    # max_concurrent_runs (the §C4 cap, known only at make_server time) --
    # the rail's concurrency-counter state (§10) needs it without waiting
    # for a 429 to learn the cap.
    snap = handler.state.snapshot()
    snap["control_enabled"] = handler.control_enabled
    snap["max_concurrent_runs"] = handler.max_concurrent_runs
    return 200, snap


@_route("GET", r"^/api/runs$")
def _get_runs(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    return 200, {"runs": handler.state.list_runs()}


@_route("GET", r"^/api/events$")
def _get_events(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    after = int(handler.query.get("after", ["0"])[0] or 0)
    return 200, {"events": handler.state.events_tail(after)}


@_route("POST", r"^/api/attach$")
def _post_attach(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    run_id = str(body.get("run_id", ""))
    ok = handler.state.attach(run_id)
    if not ok:
        return 404, {"ok": False, "error": "no such run"}
    # §C4: on a successful attach under an auth-token-protected server,
    # set the cookie the SSE endpoint will validate on every subsequent
    # same-origin request. The browser carries it automatically; an
    # EventSource request can't set custom headers, so the cookie is the
    # one way auth survives into the stream.
    if handler.auth_token:
        handler._set_auth_cookie()
    return 200, {"ok": True, "token_required": bool(handler.auth_token)}


@_route("POST", r"^/api/runs$")
def _post_runs(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    handler.require_control()
    # §C4: refuse to start a new concurrent run when the cap is reached.
    # The cap lives on the handler so a future multi-tenant dashboard could
    # set it per-virtual-host; today it's set once at make_server time.
    hosted_count = handler.state.hosted_count()
    if hosted_count >= handler.max_concurrent_runs:
        return 429, {
            "error": f"max_concurrent_runs reached ({handler.max_concurrent_runs})",
            "hosted": hosted_count,
            "max_concurrent_runs": handler.max_concurrent_runs,
        }
    body = dict(body)
    body["goal"] = _read_text_field(body.get("goal"))
    body["source"] = _read_text_field(body.get("source"))
    run_id, error = handler.state.start_run(body)
    if run_id is None:
        return 400, {"error": error}
    if handler.auth_token:
        # A new run auto-attaches the operator; set the auth cookie now so
        # the SSE stream that follows the 200 keeps working.
        handler._set_auth_cookie()
    return 200, {"run_id": run_id}


@_route("POST", r"^/api/runs/delete$")
def _post_delete_run(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    handler.require_control()
    run_id = str(body.get("run_id", ""))
    ok = handler.state.delete_run(run_id)
    return (200, {"ok": True}) if ok else (400, {"ok": False, "error": "failed to delete run"})


@_route("DELETE", r"^/api/runs/([^/]+)$")
def _delete_run(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    handler.require_control()
    run_id = unquote(match.group(1))
    ok = handler.state.delete_run(run_id)
    return (200, {"ok": True}) if ok else (400, {"ok": False, "error": "failed to delete run"})


@_route("POST", r"^/api/halt$")
def _post_halt(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    handler.require_control()
    ok = handler.state.halt(bool(body.get("value", True)))
    return (200, {"ok": True}) if ok else (409, {"ok": False, "error": "no attached run"})


@_route("POST", r"^/api/runs/kill$")
def _post_kill_run(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    """Immediately terminate the driver process for a run (SIGTERM→SIGKILL).
    Accepts an optional ``run_id``; if omitted, kills the attached run's driver."""
    handler.require_control()
    run_id = str(body.get("run_id", "")).strip() or None
    ok = handler.state.kill_run(run_id)
    return (200, {"ok": True}) if ok else (409, {"ok": False, "error": "no driver PID found for this run — it may already be stopped"})



@_route("POST", r"^/api/approvals/([^/]+)/resolve$")
def _post_resolve(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    handler.require_control()
    approval_id = unquote(match.group(1))
    ok = handler.state.resolve_approval(
        approval_id,
        action=str(body.get("action", "")),
        user_input=str(body.get("user_input", "")),
        # §DASHBOARD-UX §6.3: batch-form approvals (intake rounds) resolve
        # per question, keyed by question id.
        answers=body.get("answers"),
    )
    return (200, {"ok": True}) if ok else (409, {"ok": False, "error": "no run attached, or no such pending approval in the attached run"})


@_route("POST", r"^/api/approvals/([^/]+)/pilot-save$")
def _post_pilot_save(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    """§DASHBOARD-UX §6.2: pilot edit + approve in one browser action —
    writes the edited text back to the pilot artifact and resolves the
    pending pilot approval with the edit as ``user_input``, so the driver's
    ``approve_pilot`` derives contract rules from the diff exactly as if
    the operator had edited the file on disk and run ``approve --file``."""
    handler.require_control()
    approval_id = unquote(match.group(1))
    ok = handler.state.pilot_save(
        approval_id, str(body.get("node_id", "")), str(body.get("text", ""))
    )
    return (200, {"ok": True}) if ok else (409, {"ok": False, "error": "not a pending pilot approval for this node"})


@_route("POST", r"^/api/escalate$")
def _post_escalate(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    """§DASHBOARD-UX §11: the rail tier chip's escalate action — one tier
    up, invariant 9 (tiers only rise), same read-modify-write as the CLI's
    ``pipeline escalate``."""
    handler.require_control()
    try:
        result = handler.state.escalate()
    except (FileNotFoundError, ValueError) as exc:
        return 409, {"error": str(exc)}
    return 200, result


@_route("POST", r"^/api/node/([^/]+)/redispatch$")
def _post_redispatch(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    """§DASHBOARD-UX §11: re-dispatch one failed/blocked/stale node — a
    confirm approval whose apply half resets the node to pending with a
    fresh attempt budget."""
    handler.require_control()
    approval, reason = handler.state.request_redispatch(unquote(match.group(1)), str(body.get("reason", "")))
    return (200, approval) if approval else (400, {"error": reason or "not a redispatchable node, or no attached run"})


@_route("GET", r"^/api/node/([^/]+)/split$")
def _get_node_split(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    """§DASHBOARD-UX §11: a v7 split proposal (``scratch/<id>/split.json``)
    made operator-legible — the Node tab on a "split" parent shows what
    was proposed and why the harness accepted it."""
    detail = handler.state.node_detail(unquote(match.group(1)))
    return (200, {"proposal": detail.get("split_proposal")}) if detail is not None else (404, {"error": "not found"})


@_route("POST", r"^/api/jobs/([^/]+)/cancel$")
def _post_job_cancel(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    """§DASHBOARD-UX §11: cancel a dashboard background job (amend/triage/
    reopen/redispatch) — the jobs.jsonl record is the authority."""
    handler.require_control()
    ok = handler.state.cancel_job(unquote(match.group(1)))
    return (200, {"ok": True}) if ok else (409, {"ok": False, "error": "no attached run"})


@_route("POST", r"^/api/amend$")
def _post_amend(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    handler.require_control()
    approval = handler.state.request_amend(str(body.get("text", "")), reason=str(body.get("reason") or "web amendment"))
    return (200, approval) if approval else (400, {"error": "text is required, or no attached run"})


@_route("POST", r"^/api/reopen$")
def _post_reopen(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    handler.require_control()
    approval = handler.state.request_reopen(str(body.get("node_id", "")), str(body.get("defect", "")))
    return (200, approval) if approval else (400, {"error": "node_id and defect are required, or no attached run"})


@_route("GET", r"^/api/node/([^/]+)$")
def _get_node(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    detail = handler.state.node_detail(unquote(match.group(1)))
    return (200, detail) if detail is not None else (404, {"error": "not found"})


@_route("GET", r"^/api/node/([^/]+)/artifact$")
def _get_node_artifact(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    text = handler.state.artifact(unquote(match.group(1)))
    return (200, {"text": text}) if text is not None else (404, {"error": "not found"})


@_route("GET", r"^/api/node/([^/]+)/trace$")
def _get_node_trace(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    text = handler.state.trace(unquote(match.group(1)))
    return 200, {"text": text or ""}


@_route("GET", r"^/api/node/([^/]+)/thinking$")
def _get_node_thinking(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    """The "Thinking" tab: ``trace.jsonl`` parsed into role-tagged entries.
    §PERF: goes through ``RunState.trace_entries``'s incremental parse
    cache rather than ``rendering.parse_trace`` on the raw text — this
    route is polled every ~1.5s (after every SSE snapshot push) for as
    long as an episode runs, so re-parsing the whole trace from scratch on
    every call got slower every tick as the trace grew.

    §F1 (2026-08-12 audit): an optional ``?since=<n>`` cursor lets a caller
    fetch only ``parsed_entries[n:]`` instead of the whole (tail-capped)
    trace every tick — the live-thinking-in-the-main-feed UI polls this
    continuously while an episode runs, and re-rendering the full payload
    every ~1.5s is the exact cost this cap already exists to avoid.
    ``since`` omitted or ``0`` preserves today's exact response (the
    tail-capped full fetch, unchanged field-for-field); a positive
    ``since`` instead returns everything from that index onward,
    uncapped — the client is expected to already hold the earlier entries.
    ``next`` is always the cursor to pass on the following call, and
    ``truncated`` reuses the same cap semantics either way (whether the
    *total* trace exceeds ``MAX_THINKING_ENTRIES``, not just this
    response's slice)."""
    since_raw = (handler.query.get("since") or ["0"])[0]
    try:
        since = int(since_raw)
    except (TypeError, ValueError):
        since = 0
    since = max(0, since)
    entries = handler.state.trace_entries(unquote(match.group(1))) or []
    # B4-5 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): ts = the entry's index in
    # the trace — a monotonic key the client can sort on. The client used to
    # stamp entries with its own wall clock, so any clock skew or a slow tick
    # interleaved thinking into the feed at the wrong place relative to the
    # events' server timestamps.
    serialized = [{"role": e.role, "text": e.text, "ts": i} for i, e in enumerate(entries)]
    total = len(serialized)
    truncated = total > MAX_THINKING_ENTRIES
    if since > 0:
        return 200, {"entries": serialized[since:], "total": total, "next": total, "truncated": truncated}
    # Unchanged from before §F1: cap at the tail — live episodes only need
    # the recent turns (the client bounds its own DOM render at
    # CHAT_RENDER_CAP) — and report ``total`` so the UI can say how much
    # history was omitted.
    trimmed = serialized[-MAX_THINKING_ENTRIES:] if truncated else serialized
    return 200, {"entries": trimmed, "total": total, "next": total, "truncated": truncated}


@_route("POST", r"^/api/model/override$")
def _post_model_override(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    model = body.get("model")
    if model is not None and not isinstance(model, str):
        return 400, {"error": "model must be string or null"}
    ok = handler.state.set_model_override(model)
    if not ok:
        return 400, {"error": "no run attached"}
    return 200, {"ok": True, "model": model}


@_route("POST", r"^/api/command$")
def _post_command(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    handler.require_control()
    cmd = str(body.get("command", "")).strip()
    if not cmd:
        return 400, {"error": "command is required"}

    parts = cmd.split(maxsplit=1)
    slash_cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if slash_cmd == "/pause":
        ok = handler.state.halt(True)
        return (200, {"ok": ok, "message": "Run paused"}) if ok else (400, {"error": "no run attached"})
    elif slash_cmd == "/resume":
        ok = handler.state.halt(False)
        return (200, {"ok": ok, "message": "Run resumed"}) if ok else (400, {"error": "no run attached"})
    elif slash_cmd == "/kill":
        ok = handler.state.kill_run()
        return (200, {"ok": ok, "message": "Run killed"}) if ok else (409, {"error": "kill failed"})
    elif slash_cmd == "/tier":
        if arg.upper() not in {"T0", "T1", "T2", "T3"}:
            return 400, {"error": "tier must be T0, T1, T2, or T3"}
        ok = handler.state.set_tier_override(arg.upper())
        return (200, {"ok": ok, "message": f"Tier set to {arg.upper()}"}) if ok else (400, {"error": "no run attached"})
    elif slash_cmd == "/model":
        val = None if arg.lower() in {"", "default", "none"} else arg
        ok = handler.state.set_model_override(val)
        return (200, {"ok": ok, "message": f"Model set to {val or 'default'}"}) if ok else (400, {"error": "no run attached"})
    elif slash_cmd == "/reopen":
        if not arg:
            return 400, {"error": "node id required for /reopen"}
        subparts = arg.split(maxsplit=1)
        node_id = subparts[0]
        defect = subparts[1] if len(subparts) > 1 else ""
        ok = handler.state.reopen_node(node_id, defect)
        return (200, {"ok": ok, "message": f"Reopened {node_id}"}) if ok else (400, {"error": f"could not reopen {node_id}"})
    else:
        return 400, {"error": f"unknown command: {slash_cmd}"}


@_route("GET", r"^/api/node/([^/]+)/version/([^/]+)$")
def _get_node_version(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    text = handler.state.version_snapshot(unquote(match.group(1)), unquote(match.group(2)))
    return (200, {"text": text}) if text is not None else (404, {"error": "not found"})


@_route("GET", r"^/api/node/([^/]+)/diff/([^/]+)$")
def _get_node_diff(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    """Diff one prior version snapshot against the current artifact —
    ``rendering.diff_lines``, pre-classified into add/remove/context/
    header/hunk so the frontend styles without re-parsing unified-diff
    markers itself (same contract the deleted TUI's Diff tab relied on)."""
    node_id = unquote(match.group(1))
    tag = unquote(match.group(2))
    old_text = handler.state.version_snapshot(node_id, tag)
    if old_text is None:
        return 404, {"error": "no such version"}
    current = handler.state.artifact(node_id) or ""
    lines = [
        {"kind": line.kind, "text": line.text}
        for line in rendering.diff_lines(old_text, current, old_label=tag, new_label="current")
    ]
    return 200, {"lines": lines}


@_route("POST", r"^/api/node/([^/]+)/interject$")
def _post_interject(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    """Send a message into a *currently running* Writer/repair/research
    subagent's gptme session mid-episode (``RunState.interject``)."""
    handler.require_control()
    node_id = unquote(match.group(1))
    text = str(body.get("text") or body.get("content") or "").strip()
    ok = handler.state.interject(node_id, text)
    return (200, {"ok": True}) if ok else (409, {"ok": False, "error": "no live session found for this node"})


@_route("GET", r"^/api/spec$")
def _get_spec(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    return 200, {"text": handler.state.spec_text()}


@_route("GET", r"^/api/contract$")
def _get_contract(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    # §DASHBOARD-UX §5.3: the Doc tab's token-ceiling bar needs the contract's
    # measured size AND the ceiling it was frozen under — neither is derivable
    # client-side (the ceiling lives in v2/contract.py, the measurement in
    # v1/gates.py). Lazy imports keep this module's import surface flat.
    from ..v1.gates import estimate_tokens
    from ..v2.contract import DEFAULT_TOKEN_CEILING

    text = handler.state.contract_text()
    return 200, {"text": text, "tokens": estimate_tokens(text), "ceiling": DEFAULT_TOKEN_CEILING}


@_route("GET", r"^/api/spine$")
def _get_spine(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    return 200, {"text": handler.state.spine_text()}


@_route("GET", r"^/api/manifest$")
def _get_manifest(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    return 200, {"lines": handler.state.manifest_lines()}


@_route("GET", r"^/api/assembly$")
def _get_assembly(handler: "DashboardRequestHandler", match: Any, body: dict) -> tuple[int, Any]:
    return 200, handler.state.assembly()


def _read_text_field(raw: Any) -> str:
    """Server-side ``@path`` resolution for the ``goal``/``source`` fields
    of ``POST /api/runs`` — safely resolves file paths without blocking
    on non-existent paths, UI placeholders, or stdin."""
    s = str(raw or "").strip()
    if not s or s == "-":
        return s if s != "-" else ""
    if s.startswith("@"):
        path_str = s[1:].strip()
        if not path_str or path_str.startswith("/path/to/"):
            return ""
        path = Path(path_str)
        try:
            if not path.is_file():
                return ""
            return path.read_text(encoding="utf-8", errors="replace").strip()
        except (OSError, UnicodeError):
            return ""
    return s


class ControlDisabledError(Exception):
    pass


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server_version = "KusudaemonDashboard/1"

    # Set per-instance by ThreadingHTTPServer via the ``state``/``verbose``/
    # ``control_enabled`` attributes the server factory below stashes on
    # the class.
    state: RunState
    verbose: bool = False
    control_enabled: bool = True
    # §C4: when auth_token is non-empty, every request must carry a matching
    # cookie (set on /api/attach) or an ``Authorization: Bearer <token>``
    # header. When empty (loopback default), auth is a no-op.
    auth_token: str = ""
    # §C4: the cap on concurrently-hosted runs. Surfaces as a 429 from
    # _post_runs when the count reaches this. Set on the handler class the
    # same way ``state``/``control_enabled`` are.
    max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS

    def address_string(self) -> str:
        # §PERF: avoid socket.getfqdn reverse DNS lookups on incoming HTTP
        # requests, which can stall for 30-60s on macOS or local networks.
        return self.client_address[0]

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if self.verbose:
            super().log_message(fmt, *args)

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # -- dispatch --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        self.query = parse_qs(parts.query)

        # §C4: the static index page (and its JS) load *before* any auth
        # check would fire, exactly because the login surface itself has
        # to be reachable anonymously. /static/* is safe to serve without
        # auth (it's static, not run-state), and serving the index bare is
        # what makes the auth flow work (the client POSTs /api/attach with
        # the cookie, then the SSE handshake replays it). An authenticated
        # requester sees the same index; the difference is what /api/*
        # then returns.
        if method == "GET" and path == "/":
            self._serve_static("index.html")
            return
        if method == "GET" and path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
            return

        # Auth gate: every other route (including /api/stream) requires a
        # valid cookie or Authorization header when an auth_token is set.
        if not self._check_auth():
            self._send_json(401, {"error": "authentication required"})
            return

        if method == "GET" and path == "/api/stream":
            self._serve_stream()
            return

        if not path.startswith("/api/"):
            self._send_json(404, {"error": "not found"})
            return

        body: dict[str, Any] = {}
        if method == "POST":
            body = self._read_json_body()
            if body is None:
                self._send_json(400, {"error": "invalid JSON body"})
                return

        for route_method, pattern, fn in _ROUTES:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match is None:
                continue
            try:
                status, payload = fn(self, match, body)
            except ControlDisabledError:
                self._send_json(403, {"error": "control is disabled on this dashboard (read-only view)"})
                return
            self._send_json(status, payload)
            return
        self._send_json(404, {"error": "not found"})

    def require_control(self) -> None:
        if not self.control_enabled:
            raise ControlDisabledError()

    # -- auth (§C4) ------------------------------------------------------
    def _check_auth(self) -> bool:
        """Timing-safe auth check. When no auth_token is configured on the
        handler class, every request passes — this is the loopback default,
        where ``127.0.0.1`` binding already keeps the dashboard off the
        network. When a token is configured, a request must carry it via
        either an ``Authorization: Bearer <token>`` header (used by fetch
        callers) or the ``kusudaemon_auth`` cookie (set by /api/attach,
        used by SSE — ``EventSource`` cannot set custom headers)."""
        if not self.auth_token:
            return True
        # Cookie path — SSE EventSource and any browser fetch with default
        # ``credentials: 'same-origin'`` carry this on every same-origin
        # request.
        cookie_header = self.headers.get("Cookie", "")
        cookie_token = self._cookie_value(cookie_header, _AUTH_COOKIE_NAME)
        if cookie_token and hmac.compare_digest(cookie_token, self.auth_token):
            return True
        # Authorization: Bearer <token> — the standard path for non-SSE
        # API clients. Matches any case of ``Bearer``.
        auth_header = self.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            bearer = auth_header[7:].strip()
            if bearer and hmac.compare_digest(bearer, self.auth_token):
                # §DASHBOARD-UX §10: a Bearer-authenticated request IS the
                # login — plant the cookie right here so the very first
                # authenticated call (not just /api/attach) turns on the
                # cookie path that EventSource needs. The login overlay's
                # "validate token" call therefore also seeds the session.
                if not self._pending_set_cookie:
                    self._set_auth_cookie()
                return True
        return False

    @staticmethod
    def _cookie_value(cookie_header: str, name: str) -> str:
        """Parse a ``Cookie:`` header with ``http.cookies`` (stdlib) and
        return one named value, or ``""`` if absent. Robust against a
        malformed header — a parse failure is treated as no cookie,
        matching the conservative posture of "unknown -> deny"."""
        if not cookie_header:
            return ""
        try:
            c = http.cookies.SimpleCookie()
            c.load(cookie_header)
            return str(c[name].value) if name in c else ""
        except Exception:
            return ""

    # -- static ------------------------------------------------------------
    def _serve_static(self, rel_path: str) -> None:
        rel_path = unquote(rel_path) or "index.html"
        target = (_STATIC_DIR / rel_path).resolve()
        try:
            target.relative_to(_STATIC_DIR.resolve())
        except ValueError:
            self._send_json(403, {"error": "forbidden"})
            return
        if not target.is_file():
            self._send_json(404, {"error": "not found"})
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- SSE -----------------------------------------------------------
    def _serve_stream(self) -> None:
        # B4-3 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): do NOT "fix"
        # protocol_version to HTTP/1.1 here. This handler inherits
        # BaseHTTPRequestHandler.protocol_version = "HTTP/1.0", which is why
        # SSE works: HTTP/1.0 bodies are delimited by connection close.
        # HTTP/1.1 keep-alive would require chunked transfer-encoding, which
        # this handler does not emit, and would break the stream.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                snap = self.state.snapshot()
                snap["control_enabled"] = self.control_enabled
                snap["max_concurrent_runs"] = self.max_concurrent_runs
                payload = json.dumps(snap)
                self.wfile.write(f"event: snapshot\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(_STREAM_INTERVAL)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # -- helpers ---------------------------------------------------------
    def _read_json_body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        # B4-4 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): never let the browser's
        # heuristic cache serve a stale snapshot — the polling fallback and
        # the ?since= thinking cursor both depend on fresh responses.
        self.send_header("Cache-Control", "no-store")
        # §C4: include any pending Set-Cookie header that's been queued
        # for this response. Used by /api/attach to drop the auth cookie
        # alongside its JSON payload.
        if self._pending_set_cookie is not None:
            self.send_header("Set-Cookie", self._pending_set_cookie)
            self._pending_set_cookie = None
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # §C4: a Set-Cookie value queued by _set_auth_cookie and consumed by
    # _send_json. None when no cookie is pending.
    _pending_set_cookie: str | None = None

    def _set_auth_cookie(self) -> None:
        """Issue the auth cookie as part of the next _send_json response.
        SameSite=Strict + HttpOnly — the dashboard client never reads it
        directly; the browser carries it to every same-origin request,
        including the SSE EventSource which can't set custom headers."""
        morsel = http.cookies.Morsel()
        # Morsel.set() requires the coded_value third arg since 3.13 (it
        # used to be optional); passing the same value for both is exactly
        # what BaseCookie does internally.
        morsel.set(_AUTH_COOKIE_NAME, self.auth_token, self.auth_token)
        morsel["path"] = "/"
        morsel["httponly"] = True
        morsel["samesite"] = "Strict"
        # Same-origin only — no cross-site CSRF surface. Max-Age is 7 days
        # (a long enough window to cover an overnight or long-weekend run
        # the operator re-attaches to, short enough to expire stale
        # sessions).
        morsel["max-age"] = str(60 * 60 * 24 * 7)
        self._pending_set_cookie = morsel.OutputString()


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})


def _assert_safe_host(host: str, auth_token: str) -> None:
    """§C4: refuse to start on a non-loopback host without an auth token.

    The ``--host`` flag explicitly invites off-loopback binding, so the
    same flag's safety check is what refuses it. A non-loopback host
    exposes every artifact, every trace, every approval to any requester
    who can route to the LAN — a single ``Authorization: Bearer`` (or the
    SSE-equivalent cookie) at least gates the read surface. Loopback
    (``127.0.0.1``, ``::1``, ``localhost``) doesn't need a token: the
    binding itself is the safety.

    Raises ``ValueError`` before ``make_server`` is ever called, so the
    socket is never bound in the unsafe configuration. The error message
    names the ``--auth-token`` flag the operator needs to set, exactly the
    way ``provider_config.resolve`` names the missing env var.
    """
    if host in _LOOPBACK_HOSTS:
        return
    if not auth_token:
        raise ValueError(
            f"refusing to bind dashboard to non-loopback host {host!r} without "
            f"an auth token -- set --auth-token (and re-run with the same token, "
            f"or one a remote browser can read)"
        )


def make_server(
    state: RunState,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    control_enabled: bool = True,
    verbose: bool = False,
    auth_token: str = "",
    max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS,
) -> ThreadingHTTPServer:
    # §C4: refuse to bind off-loopback without auth. Done here, not in
    # run_forever, so any caller constructing the server directly (including
    # serve_in_background and tests) gets the same guard for free.
    _assert_safe_host(host, auth_token)
    handler_cls = type(
        "_BoundHandler",
        (DashboardRequestHandler,),
        {
            "state": state,
            "verbose": verbose,
            "control_enabled": control_enabled,
            "auth_token": auth_token,
            "max_concurrent_runs": max_concurrent_runs,
        },
    )
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    httpd.daemon_threads = True
    return httpd


def serve_in_background(
    runs_root: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    control_enabled: bool = True,
    verbose: bool = False,
    auth_token: str = "",
    max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS,
) -> tuple[ThreadingHTTPServer, RunState]:
    """Start the dashboard on a background thread; caller owns shutdown."""
    state = RunState(runs_root)
    httpd = make_server(
        state,
        host,
        port,
        control_enabled=control_enabled,
        verbose=verbose,
        auth_token=auth_token,
        max_concurrent_runs=max_concurrent_runs,
    )
    thread = threading.Thread(target=httpd.serve_forever, name="kusudaemon-dashboard", daemon=True)
    thread.start()
    return httpd, state


def run_forever(
    runs_root: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    attach_run_id: str | None = None,
    control_enabled: bool = True,
    auth_token: str = "",
    max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS,
) -> None:
    """Blocking entrypoint for ``kusudaemon serve`` / bare ``kusudaemon`` /
    ``python -m kusudaemon.dashboard.server``."""
    state = RunState(runs_root)
    if attach_run_id:
        state.attach(attach_run_id)
    httpd = make_server(
        state,
        host,
        port,
        control_enabled=control_enabled,
        verbose=True,
        auth_token=auth_token,
        max_concurrent_runs=max_concurrent_runs,
    )
    print(f"kusudaemon dashboard: http://{host}:{port}/ (watching {runs_root})")
    if not control_enabled:
        print("read-only view: control actions (attach a run to start one, approve, amend...) are disabled")
    if auth_token:
        print("auth token required: clients must send the token as a Bearer header or a kusudaemon_auth cookie")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()


def _build_parser():
    import argparse

    parser = argparse.ArgumentParser(prog="kusudaemon serve", description="Serve the recursive-decomposition web view over a runs directory.")
    parser.add_argument("--runs-root", default="~/.kusudaemon/runs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--run-id", default=None, help="Attach to this run on startup.")
    parser.add_argument("--no-control", action="store_true", help="Read-only view: disable start/attach/halt/approve/amend/reopen/interject.")
    parser.add_argument("--auth-token", default=None, help="PLAN.md §C4: token required for dashboard access when bound to a non-loopback host. Issued as a cookie at /api/attach; sent as Bearer by API clients.")
    parser.add_argument("--max-concurrent-runs", type=int, default=DEFAULT_MAX_CONCURRENT_RUNS, help="PLAN.md §C4: cap on concurrently-hosted dashboard runs (429 past this).")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    run_forever(
        args.runs_root,
        args.host,
        args.port,
        attach_run_id=args.run_id,
        control_enabled=not args.no_control,
        auth_token=args.auth_token or "",
        max_concurrent_runs=args.max_concurrent_runs,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
