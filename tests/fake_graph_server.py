"""A tiny stand-in for Meta's Graph API: it records GET requests and answers from a script of routes."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


@dataclass
class Call:
    path: str
    query: dict[str, str]


@dataclass
class FakeGraph:
    routes: dict[str, list[tuple[int, dict[str, Any]]]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def on(self, path: str, body: dict[str, Any], status: int = 200) -> None:
        self.routes.setdefault(path, []).append((status, body))

    def start(self) -> ThreadingHTTPServer:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the name http.server looks for
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                outer.calls.append(Call(parsed.path, query))
                replies = outer.routes.get(parsed.path)
                status, body = (
                    (replies.pop(0) if len(replies) > 1 else replies[0])
                    if replies
                    else (404, {"error": {"message": "no route", "code": 404}})
                )
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


@contextmanager
def fake_graph() -> Iterator[tuple[FakeGraph, str]]:
    graph = FakeGraph()
    server = graph.start()
    try:
        yield graph, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
