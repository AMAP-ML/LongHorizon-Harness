#!/usr/bin/env python3
"""Image URL proxy for Qwen-compatible WeaveBench runs.

GUI tools return PNG screenshots as base64 image blocks. Some Qwen gateways
reject large request bodies, so this proxy replaces recent base64 images with
hosted image URLs before forwarding to the upstream endpoint. It understands
both Anthropic Messages blocks and OpenAI Chat/Responses image blocks.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_UPLOAD_URL_TPL = ""
DEFAULT_SHOW_URL_TPL = ""

UPSTREAM_BASE_URL = os.environ.get("WEAVEBENCH_IMAGE_PROXY_UPSTREAM_BASE_URL", "").rstrip("/")
LISTEN_HOST = os.environ.get("WEAVEBENCH_IMAGE_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("WEAVEBENCH_IMAGE_PROXY_PORT", "8899"))
KEEP_LAST_IMAGES = int(os.environ.get("WEAVEBENCH_IMAGE_PROXY_KEEP_LAST_IMAGES", "3"))
UPLOAD_URL_TPL = os.environ.get("WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL", DEFAULT_UPLOAD_URL_TPL)
SHOW_URL_TPL = os.environ.get("WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL", DEFAULT_SHOW_URL_TPL)
UPLOAD_USER = os.environ.get("WEAVEBENCH_IMAGE_PROXY_USER", "")
UPLOAD_SIGN_SECRET = os.environ.get("WEAVEBENCH_IMAGE_PROXY_SIGN_SECRET", "")
UPLOAD_MODE = os.environ.get("WEAVEBENCH_IMAGE_PROXY_UPLOAD_MODE", "raw").strip().lower()
UPLOAD_FIELD = os.environ.get("WEAVEBENCH_IMAGE_PROXY_UPLOAD_FIELD", "file")
UPLOAD_TIMEOUT = float(os.environ.get("WEAVEBENCH_IMAGE_PROXY_UPLOAD_TIMEOUT", "30"))
UPLOAD_RETRIES = max(0, int(os.environ.get("WEAVEBENCH_IMAGE_PROXY_UPLOAD_RETRIES", "2")))
UPLOAD_RETRY_BACKOFF = float(os.environ.get("WEAVEBENCH_IMAGE_PROXY_UPLOAD_RETRY_BACKOFF", "1.0"))
UPSTREAM_RETRIES = max(0, int(os.environ.get("WEAVEBENCH_IMAGE_PROXY_UPSTREAM_RETRIES", "1")))
UPSTREAM_RETRY_BACKOFF = float(os.environ.get("WEAVEBENCH_IMAGE_PROXY_UPSTREAM_RETRY_BACKOFF", "1.0"))
LOG_PATH = os.environ.get("WEAVEBENCH_IMAGE_PROXY_LOG", "/tmp/weavebench_image_url_proxy.log")
DEBUG_REQUESTS = os.environ.get("WEAVEBENCH_IMAGE_PROXY_DEBUG_REQUESTS", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DEBUG_BODY_LIMIT = int(os.environ.get("WEAVEBENCH_IMAGE_PROXY_DEBUG_BODY_LIMIT", "4096"))
COUNT_TOKENS_CHAR_DIVISOR = max(
    1,
    int(os.environ.get("WEAVEBENCH_IMAGE_PROXY_COUNT_TOKENS_CHAR_DIVISOR", "4")),
)
COUNT_TOKENS_IMAGE_TOKENS = int(
    os.environ.get("WEAVEBENCH_IMAGE_PROXY_COUNT_TOKENS_IMAGE_TOKENS", "1000")
)
COUNT_TOKENS_TOOL_TOKENS = int(
    os.environ.get("WEAVEBENCH_IMAGE_PROXY_COUNT_TOKENS_TOOL_TOKENS", "200")
)

_cache: dict[str, str] = {}
_cache_lock = threading.Lock()
_log_lock = threading.Lock()

_DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;,]+)?;base64,(?P<data>.*)$", re.I | re.S)


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n"
    with _log_lock:
        sys.stderr.write(line)
        sys.stderr.flush()
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass


def _compute_sign(md5_hex: str) -> str:
    """Optional per-image upload signature.

    Some upload backends expect:
      sign = md5(f"{image_md5}@{user}+{secret}")
    """
    raw = f"{md5_hex}@{UPLOAD_USER}+{UPLOAD_SIGN_SECRET}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _format_url(tpl: str, image_id: str) -> str:
    return tpl.format(
        id=image_id,
        uuid=image_id,
        md5=image_id,
        user=UPLOAD_USER,
        sign=_compute_sign(image_id),
    )


def _multipart_body(field: str, filename: str, media_type: str, data: bytes) -> tuple[bytes, str]:
    boundary = "----weavebench-" + uuid.uuid4().hex
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: {media_type}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return head + data + tail, f"multipart/form-data; boundary={boundary}"


def _is_retryable_upload_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {502, 503, 504}
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        return isinstance(
            reason,
            (
                TimeoutError,
                socket.timeout,
                http.client.RemoteDisconnected,
                ConnectionResetError,
                ConnectionAbortedError,
            ),
        )
    return isinstance(
        exc,
        (
            TimeoutError,
            socket.timeout,
            http.client.RemoteDisconnected,
            ConnectionResetError,
            ConnectionAbortedError,
        ),
    )


def _retry_delay(base: float, attempt: int) -> float:
    return max(0.0, base) * (2 ** max(0, attempt - 1))


def _upload_image(data: bytes, media_type: str) -> str:
    if not UPLOAD_URL_TPL or not SHOW_URL_TPL:
        raise RuntimeError(
            "image URL proxy is enabled but no image hosting backend is configured; "
            "set WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL and "
            "WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL, or set WEAVEBENCH_IMAGE_PROXY=0"
        )

    image_id = hashlib.md5(data).hexdigest()
    with _cache_lock:
        cached = _cache.get(image_id)
    if cached:
        return cached

    upload_url = _format_url(UPLOAD_URL_TPL, image_id)
    show_url = _format_url(SHOW_URL_TPL, image_id)
    if UPLOAD_MODE == "multipart":
        body, content_type = _multipart_body(UPLOAD_FIELD, f"{image_id}.png", media_type, data)
    else:
        body, content_type = data, media_type
    req = urllib.request.Request(
        upload_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "User-Agent": "weavebench-image-url-proxy/1.0",
        },
    )
    max_attempts = UPLOAD_RETRIES + 1
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT) as resp:
                resp_body = resp.read(4096)
                if resp.status >= 400:
                    raise RuntimeError(f"upload failed status={resp.status} body={resp_body[:200]!r}")
            break
        except Exception as exc:  # noqa: BLE001
            if attempt >= max_attempts or not _is_retryable_upload_error(exc):
                raise
            delay = _retry_delay(UPLOAD_RETRY_BACKOFF, attempt)
            _log(
                "image upload retryable failure "
                f"attempt={attempt}/{max_attempts} retry_after={delay:.2f}s "
                f"error={type(exc).__name__}: {exc}"
            )
            if delay:
                time.sleep(delay)
    with _cache_lock:
        _cache[image_id] = show_url
    return show_url


def _parse_data_url(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    m = _DATA_URL_RE.match(value.strip())
    if not m:
        return None
    return m.group("data"), (m.group("mime") or "image/png")


def _base64_image_info(obj: Any) -> dict[str, str] | None:
    if not isinstance(obj, dict):
        return None
    typ = str(obj.get("type") or "").lower()

    # Anthropic Messages:
    #   {"type":"image","source":{"type":"base64","media_type":"image/png","data":"..."}}
    if typ == "image":
        src = obj.get("source")
        if (
            isinstance(src, dict)
            and src.get("type") == "base64"
            and isinstance(src.get("data"), str)
        ):
            return {
                "kind": "anthropic_image",
                "data": src.get("data", ""),
                "media_type": str(src.get("media_type") or "image/png"),
            }
        # OpenClaw tool-result native shape before provider conversion.
        if isinstance(obj.get("data"), str):
            return {
                "kind": "native_image",
                "data": str(obj.get("data")),
                "media_type": str(obj.get("mimeType") or obj.get("media_type") or "image/png"),
            }
        if isinstance(src, dict):
            parsed = _parse_data_url(src.get("url"))
            if parsed:
                data, media_type = parsed
                return {
                    "kind": "anthropic_image_urlish",
                    "data": data,
                    "media_type": media_type,
                }

    # OpenAI Chat Completions:
    #   {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}
    #   {"type":"image_url","image_url":"data:image/png;base64,..."}
    if typ == "image_url":
        image_url = obj.get("image_url")
        parsed = _parse_data_url(
            image_url.get("url") if isinstance(image_url, dict) else image_url
        )
        if parsed:
            data, media_type = parsed
            return {
                "kind": "openai_image_url",
                "data": data,
                "media_type": media_type,
            }

    # OpenAI Responses:
    #   {"type":"input_image","image_url":"data:image/png;base64,..."}
    #   {"type":"input_image","image_url":{"url":"data:image/png;base64,..."}}
    if typ == "input_image":
        image_url = obj.get("image_url") or obj.get("url")
        parsed = _parse_data_url(
            image_url.get("url") if isinstance(image_url, dict) else image_url
        )
        if parsed:
            data, media_type = parsed
            return {
                "kind": "openai_input_image",
                "data": data,
                "media_type": media_type,
            }
    return None


def _count_base64_images(obj: Any) -> int:
    if _base64_image_info(obj):
        return 1
    if isinstance(obj, dict):
        return sum(_count_base64_images(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_count_base64_images(v) for v in obj)
    return 0


def _replace_images(obj: Any, total_images: int, stats: dict[str, int]) -> Any:
    info = _base64_image_info(obj)
    if info:
        stats["seen"] += 1
        if stats["seen"] <= max(0, total_images - KEEP_LAST_IMAGES):
            stats["omitted"] += 1
            if info["kind"] == "openai_input_image":
                return {
                    "type": "input_text",
                    "text": "[old screenshot omitted by WeaveBench image URL proxy]",
                }
            return {
                "type": "text",
                "text": "[old screenshot omitted by WeaveBench image URL proxy]",
            }
        try:
            url = _upload_image(b64decode(info["data"]), info["media_type"])
        except Exception as e:  # noqa: BLE001
            stats["upload_failed"] += 1
            _log(f"image upload failed: {type(e).__name__}: {e}")
            return obj
        stats["uploaded"] += 1
        kind = info["kind"]
        if kind == "openai_image_url":
            return {"type": "image_url", "image_url": {"url": url}}
        if kind == "openai_input_image":
            return {"type": "input_image", "image_url": url}
        if kind == "native_image":
            return {"type": "image", "source": {"url": url}}
        return {"type": "image", "source": {"type": "url", "url": url}}
    if isinstance(obj, dict):
        return {k: _replace_images(v, total_images, stats) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_images(v, total_images, stats) for v in obj]
    return obj


def _rewrite_body(raw: bytes, content_type: str) -> bytes:
    if b"image" not in raw or "json" not in content_type.lower():
        return raw
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return raw
    total = _count_base64_images(body)
    if total <= 0:
        return raw
    stats = {"seen": 0, "uploaded": 0, "omitted": 0, "upload_failed": 0}
    rewritten = _replace_images(body, total, stats)
    _log(
        "images total={total} uploaded={uploaded} omitted={omitted} "
        "upload_failed={upload_failed}".format(total=total, **stats)
    )
    return json.dumps(rewritten, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _payload_summary(raw: bytes, content_type: str) -> dict[str, Any]:
    """Return a prompt-safe request summary for diagnosing upstream 4xxs."""
    summary: dict[str, Any] = {
        "bytes": len(raw),
        "content_type": content_type,
    }
    if "json" not in content_type.lower() or not raw:
        return summary
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        summary["json_error"] = type(exc).__name__
        return summary
    if not isinstance(payload, dict):
        summary["json_type"] = type(payload).__name__
        return summary

    keys = sorted(str(k) for k in payload.keys())
    known = {
        "model",
        "messages",
        "system",
        "tools",
        "tool_choice",
        "max_tokens",
        "stream",
        "temperature",
        "top_p",
        "thinking",
        "output_config",
        "metadata",
        "stop_sequences",
    }
    summary.update(
        {
            "keys": keys,
            "extra_keys": [k for k in keys if k not in known],
            "model": payload.get("model"),
            "max_tokens": payload.get("max_tokens"),
            "stream": payload.get("stream"),
            "thinking": payload.get("thinking"),
            "output_config": payload.get("output_config"),
            "messages": len(payload.get("messages") or [])
            if isinstance(payload.get("messages"), list)
            else type(payload.get("messages")).__name__,
            "tools": len(payload.get("tools") or [])
            if isinstance(payload.get("tools"), list)
            else type(payload.get("tools")).__name__,
        }
    )
    tools = payload.get("tools")
    if isinstance(tools, list):
        summary["tool_names"] = [
            str(t.get("name", ""))[:80] for t in tools[:20] if isinstance(t, dict)
        ]
    system = payload.get("system")
    if isinstance(system, list):
        summary["system_blocks"] = [
            str(block.get("type", type(block).__name__))
            for block in system[:10]
            if isinstance(block, dict)
        ]
    elif system is not None:
        summary["system_type"] = type(system).__name__
    return summary


def _is_count_tokens_request(method: str, path: str) -> bool:
    parsed = urllib.parse.urlparse(path)
    return method.upper() == "POST" and parsed.path.rstrip("/") == "/v1/messages/count_tokens"


def _is_image_like_block(obj: Any) -> bool:
    if _base64_image_info(obj):
        return True
    if not isinstance(obj, dict):
        return False
    typ = str(obj.get("type") or "").lower()
    if typ == "image":
        src = obj.get("source")
        return isinstance(src, dict) and (
            isinstance(src.get("url"), str)
            or isinstance(src.get("data"), str)
            or src.get("type") in {"url", "base64"}
        )
    if typ == "image_url":
        return isinstance(obj.get("image_url"), (str, dict))
    if typ == "input_image":
        return isinstance(obj.get("image_url") or obj.get("url"), (str, dict))
    return False


def _count_text_chars_without_image_payloads(obj: Any, stats: dict[str, int]) -> int:
    if _is_image_like_block(obj):
        stats["images"] += 1
        return 0
    if isinstance(obj, str):
        if obj.lstrip().lower().startswith("data:image/"):
            stats["images"] += 1
            return 0
        return len(obj)
    if isinstance(obj, dict):
        return sum(_count_text_chars_without_image_payloads(v, stats) for v in obj.values())
    if isinstance(obj, list):
        return sum(_count_text_chars_without_image_payloads(v, stats) for v in obj)
    return 0


def _estimate_input_tokens(raw: bytes, content_type: str) -> tuple[int, dict[str, int]]:
    stats = {"chars": 0, "images": 0, "tools": 0}
    if "json" not in content_type.lower() or not raw:
        stats["chars"] = len(raw)
    else:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            stats["chars"] = len(raw)
        else:
            if isinstance(payload, dict):
                tools = payload.get("tools")
                if isinstance(tools, list):
                    stats["tools"] = len(tools)
            stats["chars"] = _count_text_chars_without_image_payloads(payload, stats)
    text_tokens = (stats["chars"] + COUNT_TOKENS_CHAR_DIVISOR - 1) // COUNT_TOKENS_CHAR_DIVISOR
    tokens = (
        text_tokens
        + stats["images"] * COUNT_TOKENS_IMAGE_TOKENS
        + stats["tools"] * COUNT_TOKENS_TOOL_TOKENS
    )
    return max(1, tokens), stats


def _count_tokens_response(raw: bytes, content_type: str) -> bytes:
    tokens, stats = _estimate_input_tokens(raw, content_type)
    _log(
        "handled count_tokens locally input_tokens={tokens} "
        "chars={chars} images={images} tools={tools}".format(tokens=tokens, **stats)
    )
    return json.dumps({"input_tokens": tokens}, separators=(",", ":")).encode("utf-8")


def _send_proxy_transport_error(handler: BaseHTTPRequestHandler, exc: BaseException) -> None:
    body = json.dumps(
        {
            "error": {
                "type": "upstream_transport_error",
                "message": f"{type(exc).__name__}: {exc}",
            }
        },
        separators=(",", ":"),
    ).encode("utf-8")
    handler.close_connection = True
    handler.send_response(502, "Bad Gateway")
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        _log(fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        content_type = self.headers.get("Content-Type", "")
        if _is_count_tokens_request(self.command, self.path):
            body = _count_tokens_response(raw, content_type)
            self.close_connection = True
            self.send_response(200, "OK")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            return
        if not UPSTREAM_BASE_URL:
            self.send_error(500, "WEAVEBENCH_IMAGE_PROXY_UPSTREAM_BASE_URL is not set")
            return
        parsed = urllib.parse.urlparse(UPSTREAM_BASE_URL + self.path)
        body = _rewrite_body(raw, content_type) if raw else raw
        if DEBUG_REQUESTS:
            try:
                _log(
                    "request summary "
                    + json.dumps(
                        _payload_summary(body, content_type),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                _log(f"request summary failed: {type(exc).__name__}: {exc}")

        hop_by_hop = {
            "host",
            "content-length",
            "connection",
            "accept-encoding",
            "proxy-connection",
            "x-forwarded-host",
            "x-forwarded-proto",
            "x-forwarded-port",
        }
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in hop_by_hop
        }
        if body:
            headers["Content-Length"] = str(len(body))
        if parsed.netloc:
            headers["Host"] = parsed.netloc

        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        port = parsed.port
        host = parsed.hostname or ""
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        conn = None
        resp = None
        max_attempts = UPSTREAM_RETRIES + 1
        for attempt in range(1, max_attempts + 1):
            conn = conn_cls(host, port=port, timeout=300)
            try:
                conn.request(self.command, path, body=body if body else None, headers=headers)
                resp = conn.getresponse()
                break
            except (
                TimeoutError,
                socket.timeout,
                http.client.HTTPException,
                ConnectionResetError,
                ConnectionAbortedError,
                OSError,
            ) as exc:
                conn.close()
                conn = None
                if attempt >= max_attempts:
                    _log(
                        "upstream transport error "
                        f"attempt={attempt}/{max_attempts} error={type(exc).__name__}: {exc}"
                    )
                    _send_proxy_transport_error(self, exc)
                    return
                delay = _retry_delay(UPSTREAM_RETRY_BACKOFF, attempt)
                _log(
                    "upstream transport retryable failure "
                    f"attempt={attempt}/{max_attempts} retry_after={delay:.2f}s "
                    f"error={type(exc).__name__}: {exc}"
                )
                if delay:
                    time.sleep(delay)

        if conn is None or resp is None:
            self.send_error(502, "upstream transport error")
            return

        try:
            # For upstream 4xx/5xx responses, buffer the usually-small body so
            # the client gets a Content-Length and does not wait for keep-alive
            # timeout. This was the source of Claude Code's misleading
            # "400 terminated" after a five-minute hang.
            if resp.status >= 400:
                error_body = resp.read()
                self.close_connection = True
                self.send_response(resp.status, resp.reason)
                for k, v in resp.getheaders():
                    lk = k.lower()
                    if lk in ("transfer-encoding", "connection", "content-length"):
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(error_body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if error_body:
                    self.wfile.write(error_body)
                    self.wfile.flush()
                text = error_body[:DEBUG_BODY_LIMIT].decode("utf-8", errors="replace")
                _log(
                    f"upstream error status={resp.status} "
                    f"reason={resp.reason!r} body={text!r}"
                )
                return

            self.close_connection = True
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            conn.close()


def main() -> int:
    if not UPLOAD_URL_TPL or not SHOW_URL_TPL:
        _log(
            "image URL proxy is enabled but no image hosting backend is configured; "
            "set WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL and "
            "WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL, or set WEAVEBENCH_IMAGE_PROXY=0"
        )
        return 2
    _log(
        f"starting image URL proxy on {LISTEN_HOST}:{LISTEN_PORT}, "
        f"upstream={UPSTREAM_BASE_URL}, keep_last={KEEP_LAST_IMAGES}, "
        f"upload_mode={UPLOAD_MODE}"
    )
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
