"""A tiny stand-in for the Telegram Bot API: it records every call and answers from a script."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Call:
    token: str
    method: str
    fields: dict[str, Any]
    files: dict[str, bytes] = field(default_factory=dict)


def _multipart(body: bytes, boundary: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    fields: dict[str, Any] = {}
    files: dict[str, bytes] = {}
    for part in body.split(b"--" + boundary)[1:-1]:
        head, _, content = part.partition(b"\r\n\r\n")
        content = content.removesuffix(b"\r\n")
        name = re.search(rb'name="([^"]+)"', head)
        if name is None:
            continue
        if b"filename=" in head:
            files[name.group(1).decode()] = content
        else:
            fields[name.group(1).decode()] = content.decode()
    return fields, files


@dataclass
class FakeTelegram:
    calls: list[Call] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)  # handed out once by getUpdates
    errors: dict[str, tuple[int, str]] = field(default_factory=dict)  # method -> (status, description)
    next_message_id: int = 100

    def calls_to(self, method: str) -> list[Call]:
        return [c for c in self.calls if c.method == method]

    def start(self) -> ThreadingHTTPServer:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the name http.server looks for
                size = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(size)
                match = re.fullmatch(r"/bot([^/]+)/(\w+)", self.path)
                if match is None:
                    return self._reply(404, {"ok": False, "description": "Not Found"})
                token, method = match.groups()
                kind = self.headers.get("Content-Type", "")
                if kind.startswith("multipart/form-data"):
                    boundary = kind.split("boundary=")[1].encode()
                    fields, files = _multipart(body, boundary)
                else:
                    fields, files = (json.loads(body) if body else {}), {}
                outer.calls.append(Call(token, method, fields, files))
                if method in outer.errors:
                    status, description = outer.errors[method]
                    return self._reply(status, {"ok": False, "error_code": status, "description": description})
                if method == "getUpdates":
                    batch, outer.updates = outer.updates, []
                    return self._reply(200, {"ok": True, "result": batch})
                if method == "createForumTopic":
                    return self._reply(
                        200, {"ok": True, "result": {"message_thread_id": 7, "name": fields.get("name")}}
                    )
                if method in ("sendVideo", "sendMessage", "sendDocument"):
                    outer.next_message_id += 1
                    return self._reply(200, {"ok": True, "result": {"message_id": outer.next_message_id}})
                self._reply(200, {"ok": True, "result": True})

            def _reply(self, status: int, payload: dict[str, Any]) -> None:
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
def fake_telegram() -> Iterator[tuple[FakeTelegram, str]]:
    fake = FakeTelegram()
    server = fake.start()
    try:
        yield fake, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
