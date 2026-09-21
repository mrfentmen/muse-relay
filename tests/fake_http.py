#!/usr/bin/env python3
"""A local HTTP server that speaks the Upstash REST shape.

The in-process tests patch `relay_common`'s transport helpers with
FakeUpstash directly. This is the other half: a real socket, a real
`Authorization` header, a real subprocess on the other end — for the
tests that need process boundaries (argparse, exit codes, env plumbing)
or that exercise the vendored `bus-tools/` scripts, which import their
credentials themselves and cannot be patched from inside the process.

Nothing here is faked beyond the Redis itself: the requests are real HTTP
to 127.0.0.1.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_redis import FakeUpstash  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    """Upstash-shaped REST surface over FakeUpstash."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # keep test output clean

    def _send(self, body):
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.server.bearer.append(self.headers.get("Authorization", ""))
        path = self.path.lstrip("/")
        try:
            self._send(self.server.backend.api_get(path))
        except Exception as e:  # surface it the way the real API would
            self._send(json.dumps({"error": str(e)}))

    def do_POST(self):
        self.server.bearer.append(self.headers.get("Authorization", ""))
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        try:
            self._send(self.server.backend.api_post(self.path.lstrip("/"),
                                                    body))
        except Exception as e:
            self._send(json.dumps({"error": str(e)}))


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.backend = FakeUpstash()
        self.bearer = []


class FakeUpstashHTTP:
    """Start/stop wrapper; `url` is what to point MUSE_RELAY_URL at."""

    def __init__(self):
        self.server = _Server()
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def backend(self):
        return self.server.backend

    @property
    def bearer(self):
        """Every Authorization header seen, in order."""
        return self.server.bearer

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
