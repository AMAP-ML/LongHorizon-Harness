"""Default TestClient Host to loopback so Host-header hardening stays on."""

from __future__ import annotations

import sys
from urllib.parse import urljoin

import fastapi.testclient as fastapi_testclient
import pytest
import starlette.testclient as starlette_testclient

if sys.platform == "win32":

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(item):
        """Skip, rather than fail, when Windows denies symlink creation.

        Creating a symlink needs SeCreateSymbolicLinkPrivilege, which an
        ordinary account only holds under Developer Mode. The no-follow tests
        are still meaningful on Windows, so they run unchanged where the
        privilege exists and report honestly where it does not. Only the test
        body is wrapped: pytest's own tmp_path bookkeeping also makes symlinks,
        and swallowing that would skip the entire suite.
        """
        try:
            return (yield)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("symlink creation requires Developer Mode or admin rights")
            raise


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
