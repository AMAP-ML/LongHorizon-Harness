#!/usr/bin/env python3
"""Stdlib-only HTTP server compatible with the subset of OSWorld
``desktop_env`` REST endpoints actually used by SimpAgent.

Bind-mounted into the weavebench-ubuntu container by ``DockerLiteEnv``
and started inside it. Implemented routes:

  POST /setup/execute        JSON {command, shell}    -> {returncode, output, error}
  POST /setup/launch         JSON {command, shell}    -> background spawn (text)
  POST /setup/upload         multipart {file_path,file_data} -> write file
  POST /setup/download_file  JSON {url, path}         -> urllib download
  POST /file                 form/json {file_path}    -> file bytes (200) or 404
  GET  /healthz              -> "ok\\n"
"""
from __future__ import annotations

import cgi
import json
import os
import shlex
import subprocess
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

PORT = int(os.environ.get("WEAVEBENCH_LITE_PORT", "5000"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[lite-server] " + (fmt % args) + "\n")

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(n) if n > 0 else b""

    def _read_json(self) -> dict:
        try:
            return json.loads(self._read_body().decode("utf-8") or "{}")
        except Exception:
            return {}

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text, ctype: str = "text/plain") -> None:
        body = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/", "/healthz"):
            self._send_text(200, "ok\n")
            return
        self._send_text(404, "not found\n")

    def do_POST(self) -> None:
        if self.path == "/setup/execute":
            return self._execute(blocking=True)
        if self.path == "/setup/launch":
            return self._execute(blocking=False)
        if self.path == "/setup/upload":
            return self._upload()
        if self.path == "/setup/download_file":
            return self._download()
        if self.path == "/file":
            return self._fetch()
        self._send_text(404, "not found\n")

    def _execute(self, blocking: bool) -> None:
        data = self._read_json()
        cmd = data.get("command", [])
        shell = bool(data.get("shell", False))
        if isinstance(cmd, str):
            run_cmd = cmd if shell else shlex.split(cmd)
        elif isinstance(cmd, list):
            run_cmd = " ".join(cmd) if shell else cmd
        else:
            self._send_json(400, {"error": "command must be str or list"})
            return
        if not blocking:
            try:
                subprocess.Popen(
                    run_cmd, shell=shell,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL, start_new_session=True,
                )
                self._send_text(200, "launched")
            except Exception as e:
                self._send_text(500, f"launch failed: {e}")
            return
        try:
            r = subprocess.run(
                run_cmd, shell=shell,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self._send_json(200, {
                "returncode": r.returncode,
                "output": r.stdout.decode("utf-8", "replace"),
                "error":  r.stderr.decode("utf-8", "replace"),
            })
        except Exception as e:
            self._send_json(500, {"returncode": -1, "output": "", "error": str(e)})

    def _upload(self) -> None:
        ctype, _ = cgi.parse_header(self.headers.get("Content-Type", ""))
        if ctype != "multipart/form-data":
            self._send_text(400, "expected multipart/form-data")
            return
        try:
            fs = cgi.FieldStorage(
                fp=self.rfile, headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type"),
                    "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                },
                keep_blank_values=True,
            )
        except Exception as e:
            self._send_text(400, f"multipart parse failed: {e}")
            return
        file_path = fs.getvalue("file_path")
        if not file_path:
            self._send_text(400, "missing file_path")
            return
        if "file_data" not in fs:
            self._send_text(400, "missing file_data")
            return
        item = fs["file_data"]
        try:
            parent = os.path.dirname(file_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if hasattr(item, "file") and item.file is not None:
                with open(file_path, "wb") as out:
                    while True:
                        chunk = item.file.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk if isinstance(chunk, bytes)
                                  else chunk.encode("utf-8"))
            else:
                payload = item.value
                with open(file_path, "wb") as out:
                    out.write(payload if isinstance(payload, bytes)
                              else payload.encode("utf-8"))
            self._send_text(200, "OK")
        except Exception as e:
            self._send_text(500, f"upload failed: {e}")

    def _download(self) -> None:
        data = self._read_json()
        url = data.get("url")
        path = data.get("path")
        if not url or not path:
            self._send_text(400, "missing url or path")
            return
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with urllib.request.urlopen(url, timeout=600) as resp, open(path, "wb") as out:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
            self._send_text(200, "OK")
        except Exception as e:
            self._send_text(500, f"download failed: {e}")

    def _fetch(self) -> None:
        ctype, _ = cgi.parse_header(self.headers.get("Content-Type", ""))
        if ctype == "application/json":
            data = self._read_json()
            file_path = data.get("file_path")
        else:
            body = self._read_body().decode("utf-8")
            qs = parse_qs(body)
            vals = qs.get("file_path", [])
            file_path = vals[0] if vals else None
        if not file_path or not os.path.isfile(file_path):
            self._send_text(404, "not found")
            return
        try:
            with open(file_path, "rb") as f:
                payload = f.read()
            self._send_text(200, payload, ctype="application/octet-stream")
        except Exception as e:
            self._send_text(500, f"read failed: {e}")


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    sys.stderr.write(f"[lite-server] listening on 0.0.0.0:{PORT}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
