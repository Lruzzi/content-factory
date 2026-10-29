"""A tiny stand-in for an S3-compatible store (Cloudflare R2): path-style PUT, GET, HEAD and DELETE of objects."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse


@dataclass
class FakeS3:
    objects: dict[str, bytes] = field(default_factory=dict)  # "bucket/key" -> content
    calls: list[tuple[str, str]] = field(default_factory=list)  # (method, "bucket/key")
    fail: dict[str, int] = field(default_factory=dict)  # method -> status to answer with
    private: bool = True  # a read needs a signature (as the real S3 address does); False models a public bucket

    def start(self) -> ThreadingHTTPServer:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _key(self) -> str:
                return unquote(urlparse(self.path).path.lstrip("/"))

            def _send(self, status: int, body: bytes = b"") -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("ETag", '"fake"')
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method: str) -> None:
                size = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(size) if size else b""
                key = self._key()
                outer.calls.append((method, key))
                if method in outer.fail:
                    return self._send(outer.fail[method])
                signed = "X-Amz-Signature" in self.path or "Authorization" in self.headers
                if method in ("GET", "HEAD") and outer.private and not signed:
                    return self._send(403)
                if method == "PUT":
                    outer.objects[key] = body
                    return self._send(200)
                if method == "DELETE":
                    outer.objects.pop(key, None)
                    return self._send(204)
                if key not in outer.objects:
                    return self._send(404)
                self._send(200, outer.objects[key] if method == "GET" else b"")

            def do_PUT(self) -> None:  # noqa: N802
                self._handle("PUT")

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def do_HEAD(self) -> None:  # noqa: N802
                self._handle("HEAD")

            def do_DELETE(self) -> None:  # noqa: N802
                self._handle("DELETE")

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


@contextmanager
def fake_s3() -> Iterator[tuple[FakeS3, str]]:
    fake = FakeS3()
    server = fake.start()
    try:
        yield fake, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
