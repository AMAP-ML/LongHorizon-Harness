"""Standalone HTTP server for the LongHorizon-Harness dashboard.

Uses only the Python standard library (no extra dependencies). All front-end
assets live as static files under ``static/`` (index.html, style.css, app.js) —
no HTML/CSS/JS is embedded in Python. Serves:

* ``GET  /`` and ``/static/*``            -> static front-end assets
* ``GET  /api/state``                     -> full snapshot (report, rounds, ...)
* ``GET  /api/round/{n}``                 -> artifact list for a round
* ``GET  /api/round/{n}/{name}``          -> raw artifact text
* ``GET  /api/round/{n}/trajectory/{role}`` -> parsed Claude trajectory
* ``POST /api/approvals/{id}/resolve``    -> resolve a pending approval
* ``POST /api/inject``                    -> queue a free-form operator note
* ``POST /api/select-run``                -> switch the browsed run
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .state import DashboardState

_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

_ROUND_LIST_RE = re.compile(r"^/api/round/(\d+)/?$")
_TRAJECTORY_RE = re.compile(r"^/api/round/(\d+)/trajectory/([a-z_]+)/?$")
_ROUND_FILE_RE = re.compile(r"^/api/round/(\d+)/([^/]+)$")
_RESOLVE_RE = re.compile(r"^/api/approvals/([A-Za-z0-9]+)/resolve/?$")


class DashboardHandle:
    """Handle to a running background dashboard server."""

    def __init__(self, server: ThreadingHTTPServer, thread: threading.Thread, state: DashboardState) -> None:
        self._server = server
        self._thread = thread
        self.state = state

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def shutdown(self) -> None:
        try:
            self._server.shutdown()
        finally:
            self._server.server_close()

    def serve_forever_blocking(self) -> None:
        """Block the calling thread until interrupted (for the CLI command)."""
        try:
            self._thread.join()
        except KeyboardInterrupt:
            self.shutdown()


def start_dashboard(
    log_dir: str | None = None,
    *,
    port: int = 0,
    host: str = "127.0.0.1",
    task: str = "",
    runs_root: str | None = None,
    state: DashboardState | None = None,
    control_enabled: bool | None = None,
) -> DashboardHandle:
    """Start the dashboard HTTP server in a background daemon thread.

    Pass ``runs_root`` (e.g. ``./.lh-harness/runs``) to let the operator browse
    and switch between all runs from the UI. Pass ``log_dir`` to pin one run's logs.
    """

    dashboard_state = state or DashboardState(
        log_dir,
        task=task,
        runs_root=runs_root,
        control_enabled=bool(task) if control_enabled is None else control_enabled,
    )

    handler = _make_handler(dashboard_state)
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="lh-harness-dashboard", daemon=True)
    thread.start()
    return DashboardHandle(server, thread, dashboard_state)


def _make_handler(state: DashboardState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "LongHorizonHarnessDashboard/1.0"

        def log_message(self, *args: Any) -> None:  # noqa: D401 - silence default logging
            return

        # -- helpers -------------------------------------------------------
        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, rel: str) -> None:
            # Serve a file from the static/ dir. Plain filenames only (no
            # subdirs, no traversal) since all assets are flat.
            name = rel.lstrip("/") or "index.html"
            if "/" in name or "\\" in name or name in {"", ".", ".."}:
                self._send_text("not found", status=404)
                return
            target = (_STATIC_DIR / name).resolve()
            try:
                target.relative_to(_STATIC_DIR.resolve())
            except ValueError:
                self._send_text("not found", status=404)
                return
            if not target.is_file():
                self._send_text("not found", status=404)
                return
            content_type = _STATIC_TYPES.get(target.suffix.lower(), "application/octet-stream")
            try:
                data = target.read_bytes()
            except OSError:
                self._send_text("not found", status=404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            # Front-end assets change with the harness; never let the browser
            # reuse a stale copy (otherwise UI fixes appear not to take effect).
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(data)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        # -- routing -------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send_static("index.html")
                return
            if path.startswith("/static/"):
                self._send_static(path[len("/static/"):])
                return
            if path == "/api/state":
                self._send_json(state.snapshot())
                return
            match = _TRAJECTORY_RE.match(path)
            if match:
                round_index = int(match.group(1))
                role = match.group(2)
                traj = state.read_trajectory(round_index, role)
                if traj is None:
                    self._send_json({"error": "not found"}, status=404)
                else:
                    self._send_json(traj)
                return
            match = _ROUND_FILE_RE.match(path)
            if match:
                round_index = int(match.group(1))
                name = match.group(2)
                content = state.read_round_artifact(round_index, name)
                if content is None:
                    self._send_json({"error": "not found"}, status=404)
                else:
                    self._send_text(content)
                return
            match = _ROUND_LIST_RE.match(path)
            if match:
                round_index = int(match.group(1))
                self._send_json({"round_index": round_index, "artifacts": state.list_round_artifacts(round_index)})
                return
            self._send_json({"error": "not found"}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            match = _RESOLVE_RE.match(path)
            if match:
                approval_id = match.group(1)
                body = self._read_json_body()
                action = str(body.get("action", "continue"))
                # Accept the new field name; keep "instructions" for compatibility.
                user_input = str(body.get("user_input", body.get("instructions", "")))
                reason = str(body.get("reason", ""))
                ok = state.resolve_approval(
                    approval_id, action=action, reason=reason, user_input=user_input
                )
                self._send_json({"ok": ok}, status=200 if ok else 404)
                return
            if path == "/api/inject":
                body = self._read_json_body()
                ok = state.add_injection(str(body.get("instructions", "")))
                self._send_json({"ok": ok}, status=200 if ok else 400)
                return
            if path == "/api/select-run":
                body = self._read_json_body()
                ok = state.select_run(str(body.get("run_id", "")))
                self._send_json({"ok": ok}, status=200 if ok else 404)
                return
            self._send_json({"error": "not found"}, status=404)

    return Handler
