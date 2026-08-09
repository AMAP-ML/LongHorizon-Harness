"""Stdlib HTTP server mounting :class:`RecursiveRunState` (PLAN.md §11's
"View surface: local web app").

No new dependency: routing, JSON, and static-file serving are all
``http.server``/``urllib``/``json`` from the standard library, matching
the rest of the harness's "stdlib + packaging/tomli" rule. One
``ThreadingHTTPServer`` so a long-lived SSE connection (``/api/stream``)
never blocks any other request.

This module owns *transport* only — request parsing, routing, JSON
encoding, path-traversal-safe static serving. Every read or mutation is a
single call into ``RecursiveRunState``, which is itself the authority on
what is safe to do to a run directory (id validation, ``control_enabled``
gating for the mutating calls it implements). Routes this module adds on
top of state (``halt``, ``request_amend``, ``request_reopen``) are gated
here with the same ``control_enabled`` flag for defense in depth, since
those three state methods don't gate themselves.

Two ways to reach this server (PLAN.md §11: "a separate process watching
the run directory... can be attached from anywhere"):

* ``kusudaemon serve --runs-root <dir>`` — the primary, standalone view
  surface; a `run`/`resume`/`--detach` process needs nothing from this
  module to make progress, so the server can start, stop, or crash without
  ever touching a run.
* ``kusudaemon run --dashboard`` — the same server started in a background
  thread of the (foreground) driver process, auto-attached to that run,
  purely as a convenience so a single command gives you both.
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from .recursive import RecursiveRunState

_STATIC_DIR = Path(__file__).parent / "static"
_STREAM_INTERVAL = 1.5

_Handler = Callable[["DashboardRequestHandler", re.Match, dict[str, Any]], tuple[int, Any]]


def _route(method: str, pattern: str) -> Callable[[_Handler], _Handler]:
    def register(fn: _Handler) -> _Handler:
        _ROUTES.append((method, re.compile(pattern), fn))
        return fn

    return register


_ROUTES: list[tuple[str, re.Pattern, _Handler]] = []


@_route("GET", r"^/api/snapshot$")
def _get_snapshot(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    return 200, handler.state.snapshot()


@_route("GET", r"^/api/runs$")
def _get_runs(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    return 200, {"runs": handler.state.list_runs()}


@_route("GET", r"^/api/events$")
def _get_events(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    after = int(handler.query.get("after", ["0"])[0] or 0)
    return 200, {"events": handler.state.events_tail(after)}


@_route("POST", r"^/api/attach$")
def _post_attach(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    run_id = str(body.get("run_id", ""))
    ok = handler.state.attach(run_id)
    return (200, {"ok": True}) if ok else (404, {"ok": False, "error": "no such run"})


@_route("POST", r"^/api/runs$")
def _post_runs(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    handler.require_control()
    run_id, error = handler.state.start_run(body)
    if run_id is None:
        return 400, {"error": error}
    return 200, {"run_id": run_id}


@_route("POST", r"^/api/halt$")
def _post_halt(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    handler.require_control()
    ok = handler.state.halt(bool(body.get("value", True)))
    return (200, {"ok": True}) if ok else (409, {"ok": False, "error": "no attached run"})


@_route("POST", r"^/api/approvals/([^/]+)/resolve$")
def _post_resolve(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    handler.require_control()
    approval_id = unquote(match.group(1))
    ok = handler.state.resolve_approval(
        approval_id, action=str(body.get("action", "")), user_input=str(body.get("user_input", ""))
    )
    return (200, {"ok": True}) if ok else (409, {"ok": False, "error": "not pending or no attached run"})


@_route("POST", r"^/api/amend$")
def _post_amend(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    handler.require_control()
    approval = handler.state.request_amend(str(body.get("text", "")), reason=str(body.get("reason") or "web amendment"))
    return (200, approval) if approval else (400, {"error": "text is required, or no attached run"})


@_route("POST", r"^/api/reopen$")
def _post_reopen(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    handler.require_control()
    approval = handler.state.request_reopen(str(body.get("node_id", "")), str(body.get("defect", "")))
    return (200, approval) if approval else (400, {"error": "node_id and defect are required, or no attached run"})


@_route("GET", r"^/api/node/([^/]+)/artifact$")
def _get_node_artifact(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    text = handler.state.artifact(unquote(match.group(1)))
    return (200, {"text": text}) if text is not None else (404, {"error": "not found"})


@_route("GET", r"^/api/node/([^/]+)/trace$")
def _get_node_trace(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    text = handler.state.trace(unquote(match.group(1)))
    return 200, {"text": text or ""}


@_route("GET", r"^/api/node/([^/]+)/version/([^/]+)$")
def _get_node_version(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    text = handler.state.version_snapshot(unquote(match.group(1)), unquote(match.group(2)))
    return (200, {"text": text}) if text is not None else (404, {"error": "not found"})


@_route("GET", r"^/api/node/([^/]+)$")
def _get_node(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    detail = handler.state.node_detail(unquote(match.group(1)))
    return (200, detail) if detail is not None else (404, {"error": "not found"})


@_route("GET", r"^/api/spec$")
def _get_spec(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    return 200, {"text": handler.state.spec_text()}


@_route("GET", r"^/api/contract$")
def _get_contract(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    return 200, {"text": handler.state.contract_text()}


@_route("GET", r"^/api/spine$")
def _get_spine(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    return 200, {"text": handler.state.spine_text()}


@_route("GET", r"^/api/manifest$")
def _get_manifest(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    return 200, {"lines": handler.state.manifest_lines()}


@_route("GET", r"^/api/assembly$")
def _get_assembly(handler: "DashboardRequestHandler", match: re.Match, body: dict) -> tuple[int, Any]:
    return 200, handler.state.assembly()


class ControlDisabledError(Exception):
    pass


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server_version = "KusudaemonDashboard/1"

    # Set per-instance by ThreadingHTTPServer via the ``state``/``verbose``
    # attributes the server factory below stashes on the class.
    state: RecursiveRunState
    verbose: bool = False

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

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        self.query = parse_qs(parts.query)

        if method == "GET" and path == "/":
            self._serve_static("index.html")
            return
        if method == "GET" and path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
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
        if not self.state.control_enabled:
            raise ControlDisabledError()

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
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                payload = json.dumps(self.state.snapshot())
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
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(
    state: RecursiveRunState, host: str = "127.0.0.1", port: int = 8765, *, verbose: bool = False
) -> ThreadingHTTPServer:
    handler_cls = type("_BoundHandler", (DashboardRequestHandler,), {"state": state, "verbose": verbose})
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    httpd.daemon_threads = True
    return httpd


def serve_in_background(
    runs_root: str, host: str = "127.0.0.1", port: int = 8765, *, control_enabled: bool = True, verbose: bool = False
) -> tuple[ThreadingHTTPServer, RecursiveRunState]:
    """Start the dashboard on a background thread; caller owns shutdown."""
    state = RecursiveRunState(runs_root, control_enabled=control_enabled)
    httpd = make_server(state, host, port, verbose=verbose)
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
) -> None:
    """Blocking entrypoint for ``kusudaemon serve`` / ``python -m
    kusudaemon.dashboard.server``."""
    state = RecursiveRunState(runs_root, control_enabled=control_enabled)
    if attach_run_id:
        state.attach(attach_run_id)
    httpd = make_server(state, host, port, verbose=True)
    print(f"kusudaemon dashboard: http://{host}:{port}/ (watching {runs_root})")
    if not control_enabled:
        print("read-only view: control actions (attach a run to start one, approve, amend...) are disabled")
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
    parser.add_argument("--runs-root", default="./.kusudaemon/runs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--run-id", default=None, help="Attach to this run on startup.")
    parser.add_argument("--no-control", action="store_true", help="Read-only view: disable start/attach/approve/amend/halt/reopen.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    run_forever(
        args.runs_root, args.host, args.port, attach_run_id=args.run_id, control_enabled=not args.no_control
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
