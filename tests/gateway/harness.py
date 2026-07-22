"""Capture sink for G2's test fixtures.

A local HTTP listener that records the raw method/url/headers/body of every
request it receives, so the SigV4-verify and classify tests exercise REAL
boto3-signed requests instead of hand-built ones (ported from the research
prototype's capture sink, `.superpowers/sdd/research-iam-gateway.md` §Q1/Q2
-- its own scratchpad `capture.py` is gone; this is a from-scratch
reimplementation of the same idea, not a port of surviving code).

Point a boto3 client at `CaptureSink.endpoint` with throwaway credentials,
make the call through `sink.call(lambda: client.some_op(...))` -- which
swallows whatever boto3 raises trying to parse the sink's placeholder
response, since the request was already captured before that -- and get
back the `CapturedRequest` boto3 actually put on the wire.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class CapturedRequest:
    """One request exactly as boto3 signed and sent it."""

    method: str
    url: str  # scheme + host + RAW (percent-encoded) path + query string
    headers: dict[str, str]
    body: bytes


class _CaptureHandler(BaseHTTPRequestHandler):
    # HTTP/1.0 (the base class default) never auto-replies "100 Continue",
    # so a PUT/POST with a body sits out urllib3's ~1s Expect:100-continue
    # fallback timeout on every single call -- HTTP/1.1 fixes that.
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # silence test-run noise
        return

    def _capture(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = dict(self.headers.items())
        host = headers.get("Host") or "%s:%d" % self.server.server_address[:2]
        url = f"http://{host}{self.path}"
        self.server.sink.requests.append(  # type: ignore[attr-defined]
            CapturedRequest(method=self.command, url=url, headers=headers, body=body)
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = _capture
    do_PUT = _capture
    do_POST = _capture
    do_DELETE = _capture
    do_HEAD = _capture


class CaptureSink:
    """A throwaway HTTP server that records every request it receives."""

    def __init__(self) -> None:
        self.requests: list[CapturedRequest] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
        self._server.sink = self  # type: ignore[attr-defined]
        # A short poll_interval matters here: shutdown() waits up to one
        # interval for serve_forever's loop to notice it, and that runs on
        # every single test's teardown -- the 0.5s default adds up fast.
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()

    @property
    def endpoint(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def call(self, fn: Callable[[], Any]) -> CapturedRequest:
        before = len(self.requests)
        try:
            fn()
        except Exception:
            pass
        return self.requests[before]

    def close(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=2)
