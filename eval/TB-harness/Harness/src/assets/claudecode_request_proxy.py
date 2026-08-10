#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import json
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

REQUEST_HEADERS_TO_SKIP = HOP_BY_HOP_HEADERS | {"accept-encoding"}

REQUEST_AUDIT_KEYS = (
    "model",
    "max_tokens",
    "temperature",
    "top_p",
    "top_k",
    "enable_thinking",
    "thinking",
    "stream",
)


class ProxyConfig:
    def __init__(
        self,
        *,
        upstream: str,
        overrides: dict[str, Any],
        dashscope_wait_timeout: str | None,
        log_file: Path | None,
        requests_log_file: Path | None,
    ) -> None:
        self.upstream = upstream.rstrip("/")
        self.overrides = overrides
        self.dashscope_wait_timeout = dashscope_wait_timeout
        self.log_file = log_file
        self.requests_log_file = requests_log_file
        self.lock = threading.Lock()

    def log(self, message: str) -> None:
        line = f"[claude-code-proxy] {message}\n"
        with self.lock:
            if self.log_file is not None:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                with self.log_file.open("a", encoding="utf-8") as handle:
                    handle.write(line)
            else:
                sys.stderr.write(line)

    def log_request_event(self, event: dict[str, Any]) -> None:
        if self.requests_log_file is None:
            return
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        with self.lock:
            self.requests_log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.requests_log_file.open("a", encoding="utf-8") as handle:
                handle.write(line)


class RequestProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    config: ProxyConfig

    def log_message(self, fmt: str, *args: Any) -> None:
        self.config.log(fmt % args)

    def do_GET(self) -> None:
        self._forward()

    def do_HEAD(self) -> None:
        if self.path == "/":
            self.send_response(200)
            self.send_header("connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def _forward(self) -> None:
        body = self._read_body()
        self.config.log(f"forwarding {self.command} {self.path}")

        upstream_url = urllib.parse.urljoin(self.config.upstream + "/", self.path.lstrip("/"))
        parsed = urllib.parse.urlparse(upstream_url)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query

        body, changed, audit = rewrite_body(
            body,
            overrides=self.config.overrides,
        )
        if audit is not None:
            self.config.log_request_event(
                {
                    "event": "request",
                    "path": self.path,
                    "upstream": upstream_url,
                    "before": audit["before"],
                    "after": audit["after"],
                }
            )
        if changed:
            self.config.log(f"rewrote request body for {self.path}")

        headers = self._forward_headers(body)
        conn = make_connection(parsed)
        try:
            conn.request(self.command, target, body=body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() not in HOP_BY_HOP_HEADERS:
                    self.send_header(key, value)
            self.send_header("connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                self.close_connection = True
                return
            error_body_head = bytearray()
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                if resp.status >= 400 and len(error_body_head) < 4096:
                    remaining = 4096 - len(error_body_head)
                    error_body_head.extend(chunk[:remaining])
                self.wfile.write(chunk)
                self.wfile.flush()
            if resp.status >= 400:
                self.config.log_request_event(
                    {
                        "event": "upstream_http_error",
                        "path": self.path,
                        "upstream": upstream_url,
                        "status": resp.status,
                        "reason": resp.reason,
                        "body_head": error_body_head.decode("utf-8", errors="replace"),
                    }
                )
            self.close_connection = True
        except Exception as exc:
            self.config.log(f"upstream request failed: {exc!r}")
            self.config.log_request_event(
                {
                    "event": "upstream_error",
                    "path": self.path,
                    "upstream": upstream_url,
                    "error": repr(exc),
                }
            )
            payload = json.dumps(
                {"type": "error", "error": {"type": "proxy_error", "message": str(exc)}}
            ).encode("utf-8")
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.send_header("connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True
        finally:
            conn.close()

    def _read_body(self) -> bytes:
        length = int(self.headers.get("content-length") or "0")
        return self.rfile.read(length) if length > 0 else b""

    def _forward_headers(self, body: bytes) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            if key.lower() not in REQUEST_HEADERS_TO_SKIP:
                headers[key] = value
        headers["accept-encoding"] = "identity"
        if self.config.dashscope_wait_timeout:
            headers["X-DashScope-Wait-Timeout"] = self.config.dashscope_wait_timeout
        if body:
            headers["content-length"] = str(len(body))
        return headers


def make_connection(parsed: urllib.parse.ParseResult) -> http.client.HTTPConnection:
    host = parsed.hostname
    if not host:
        raise ValueError(f"Invalid upstream URL: {parsed.geturl()}")
    port = parsed.port
    if parsed.scheme == "https":
        context = ssl.create_default_context()
        return http.client.HTTPSConnection(host, port=port, timeout=600, context=context)
    if parsed.scheme == "http":
        return http.client.HTTPConnection(host, port=port, timeout=600)
    raise ValueError(f"Unsupported upstream scheme: {parsed.scheme}")


def rewrite_body(
    body: bytes,
    *,
    overrides: dict[str, Any],
) -> tuple[bytes, bool, dict[str, dict[str, Any]] | None]:
    if not body:
        return body, False, None

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return body, False, None

    if not isinstance(payload, dict):
        return body, False, None

    changed = False
    next_payload = dict(payload)
    before = request_audit_payload(payload)

    for key, value in overrides.items():
        if value is None:
            continue
        if next_payload.get(key) != value:
            next_payload[key] = value
            changed = True

    after = request_audit_payload(next_payload)
    audit = {"before": before, "after": after}
    if not changed:
        return body, False, audit

    return (
        json.dumps(next_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        True,
        audit,
    )


def request_audit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in REQUEST_AUDIT_KEYS}


def parse_overrides(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}

    out: dict[str, Any] = {}
    for key, value in parsed.items():
        if key == "model_info" or value is None:
            continue
        if key == "extra_body" and isinstance(value, dict):
            for extra_key, extra_value in value.items():
                if extra_value is not None:
                    out[str(extra_key)] = extra_value
            continue
        out[str(key)] = value
    return out


def parse_dashscope_wait_timeout(raw: str | None) -> str | None:
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return str(value)


def write_ready_file(path: Path, host: str, port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"ANTHROPIC_BASE_URL='http://{host}:{port}'\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--log-file")
    parser.add_argument("--requests-log-file")
    parser.add_argument("--overrides", default="")
    parser.add_argument("--dashscope-wait-timeout", default="")
    args = parser.parse_args()

    RequestProxyHandler.config = ProxyConfig(
        upstream=args.upstream,
        overrides=parse_overrides(args.overrides),
        dashscope_wait_timeout=parse_dashscope_wait_timeout(args.dashscope_wait_timeout),
        log_file=Path(args.log_file) if args.log_file else None,
        requests_log_file=Path(args.requests_log_file) if args.requests_log_file else None,
    )

    server = ThreadingHTTPServer((args.listen_host, args.port), RequestProxyHandler)
    host, port = server.server_address[:2]
    write_ready_file(Path(args.ready_file), str(host), int(port))
    RequestProxyHandler.config.log(
        f"listening on {host}:{port}, upstream={args.upstream}, "
        f"dashscope_wait_timeout={RequestProxyHandler.config.dashscope_wait_timeout or 'disabled'}"
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
