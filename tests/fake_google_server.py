"""A tiny stand-in for Google's OAuth token endpoint and the YouTube upload API. It records every call."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

SCOPE = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


@dataclass
class Call:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    form: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass
class FakeGoogle:
    calls: list[Call] = field(default_factory=list)
    good_code: str = "good-code"
    refresh_token: str = "rt-1"  # noqa: S105
    grant_scope: str = SCOPE
    send_refresh_token: bool = True
    revoked: bool = False
    # --- the YouTube upload API ---
    init_error: tuple[int, str] | None = None  # (status, reason) for starting an upload, e.g. (403, "quotaExceeded")
    put_failures: int = 0  # the next N chunk uploads answer 503 without keeping anything
    fail_puts_from: int | None = None  # every chunk upload from this one (counting from 0) answers 503
    data_puts: int = 0
    video_id: str = "vid123"
    privacy_given: str | None = None  # what videos.list reports; defaults to what the upload asked for
    uploads: dict[str, bytearray] = field(default_factory=dict)
    metadata: list[dict[str, Any]] = field(default_factory=list)
    base: str = ""

    def calls_to(self, path: str) -> list[Call]:
        return [c for c in self.calls if c.path == path]

    def start(self) -> ThreadingHTTPServer:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _read(self, method: str) -> Call:
                parsed = urlparse(self.path)
                size = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(size) if size else b""
                form: dict[str, str] = {}
                if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
                    form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
                call = Call(
                    method,
                    parsed.path,
                    {k: v[0] for k, v in parse_qs(parsed.query).items()},
                    dict(self.headers),
                    form,
                    body,
                )
                outer.calls.append(call)
                return call

            def do_POST(self) -> None:  # noqa: N802 - the name http.server looks for
                call = self._read("POST")
                if call.path == "/token":
                    return self._token(call)
                if call.path == "/upload/youtube/v3/videos":
                    return self._start_upload(call)
                self._json(404, {"error": "not found"})

            def do_PUT(self) -> None:  # noqa: N802
                call = self._read("PUT")
                session = call.path.rsplit("/", 1)[-1]
                if session not in outer.uploads:
                    return self._json(404, {"error": "no such session"})
                total = int(call.headers["Content-Range"].rsplit("/", 1)[1])
                have = outer.uploads[session]
                if call.headers["Content-Range"].startswith("bytes */"):  # "how much do you have?"
                    if len(have) >= total:  # everything is there: answer with the finished video
                        return self._json(200, {"id": outer.video_id, "status": {"privacyStatus": "private"}})
                    return self._progress(have, total)
                outer.data_puts += 1
                if outer.fail_puts_from is not None and outer.data_puts - 1 >= outer.fail_puts_from:
                    return self._json(503, {"error": {"code": 503, "message": "backend error"}})
                if outer.put_failures > 0:
                    outer.put_failures -= 1
                    return self._json(503, {"error": {"code": 503, "message": "backend error"}})
                start = int(call.headers["Content-Range"].split(" ")[1].split("-")[0])
                if start != len(have):
                    return self._json(400, {"error": {"code": 400, "message": "range mismatch"}})
                have.extend(call.body)
                if len(have) < total:
                    return self._progress(have, total)
                asked = outer.metadata[-1].get("status", {}).get("privacyStatus")
                self._json(200, {"id": outer.video_id, "status": {"privacyStatus": asked}})

            def do_GET(self) -> None:  # noqa: N802
                call = self._read("GET")
                if call.path == "/youtube/v3/videos":
                    asked = outer.metadata[-1].get("status", {}).get("privacyStatus") if outer.metadata else None
                    status = {"privacyStatus": outer.privacy_given or asked, "uploadStatus": "processed"}
                    items = (
                        [{"id": call.query.get("id"), "status": status}]
                        if call.query.get("id") == outer.video_id
                        else []
                    )
                    return self._json(200, {"items": items})
                if call.path == "/youtube/v3/channels":
                    return self._json(200, {"items": [{"id": "UCtest", "snippet": {"title": "Test Channel"}}]})
                self._json(404, {"error": "not found"})

            def _start_upload(self, call: Call) -> None:
                if outer.init_error:
                    status, reason = outer.init_error
                    return self._json(
                        status, {"error": {"code": status, "errors": [{"reason": reason}], "message": reason}}
                    )
                outer.metadata.append(json.loads(call.body))
                session = f"s{len(outer.uploads) + 1}"
                outer.uploads[session] = bytearray()
                self.send_response(200)
                self.send_header("Location", f"{outer.base}/upload/session/{session}")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _progress(self, have: bytearray, total: int) -> None:
                self.send_response(308)
                if have:
                    self.send_header("Range", f"bytes=0-{len(have) - 1}")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _token(self, call: Call) -> None:
                grant = call.form.get("grant_type")
                if grant == "authorization_code" and call.form.get("code") == outer.good_code:
                    out: dict[str, Any] = {
                        "access_token": "at-1", "expires_in": 3599, "scope": outer.grant_scope, "token_type": "Bearer",
                    }  # fmt: skip
                    if outer.send_refresh_token:
                        out["refresh_token"] = outer.refresh_token
                    return self._json(200, out)
                if (
                    grant == "refresh_token"
                    and call.form.get("refresh_token") == outer.refresh_token
                    and not outer.revoked
                ):
                    return self._json(200, {"access_token": "at-2", "expires_in": 3599, "token_type": "Bearer"})
                self._json(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


@contextmanager
def fake_google() -> Iterator[tuple[FakeGoogle, str]]:
    fake = FakeGoogle()
    server = fake.start()
    fake.base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield fake, fake.base
    finally:
        server.shutdown()
        server.server_close()
