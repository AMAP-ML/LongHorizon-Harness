"""Default TestClient Host to loopback so Host-header hardening stays on."""

from __future__ import annotations

from urllib.parse import urljoin

import fastapi.testclient as fastapi_testclient
import starlette.testclient as starlette_testclient

_Orig = starlette_testclient.TestClient
_Upgrade = starlette_testclient._Upgrade


class LoopbackTestClient(_Orig):
    def __init__(self, app, *args, **kwargs):
        kwargs.setdefault("base_url", "http://127.0.0.1")
        super().__init__(app, *args, **kwargs)

    def websocket_connect(self, url, subprotocols=None, **kwargs):
        # Starlette hardcodes ws://testserver; rewrite so Host stays loopback.
        url = urljoin("ws://127.0.0.1", url)
        headers = kwargs.get("headers", {})
        headers.setdefault("connection", "upgrade")
        headers.setdefault("sec-websocket-key", "testserver==")
        headers.setdefault("sec-websocket-version", "13")
        if subprotocols is not None:
            headers.setdefault("sec-websocket-protocol", ", ".join(subprotocols))
        kwargs["headers"] = headers
        try:
            super().request("GET", url, **kwargs)
        except _Upgrade as exc:
            return exc.session
        raise RuntimeError("Expected WebSocket upgrade")


starlette_testclient.TestClient = LoopbackTestClient
fastapi_testclient.TestClient = LoopbackTestClient
