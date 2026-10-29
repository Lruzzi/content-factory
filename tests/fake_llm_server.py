"""A tiny HTTP server that plays an LLM provider: it records requests and replies from a script."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Reply:
    status: int = 200
    body: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    delay_s: float = 0.0


@dataclass
class Seen:
    path: str
    headers: dict[str, str]
    body: dict[str, Any]


def openai_reply(text: str | None, prompt_tokens: int = 12, completion_tokens: int = 5, finish: str = "stop") -> Reply:
    message = {"role": "assistant", "content": text}
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "fake-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if prompt_tokens >= 0:
        body["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    return Reply(200, body)


def anthropic_reply(
    text: str,
    input_tokens: int = 12,
    output_tokens: int = 5,
    stop_reason: str = "end_turn",
    extra_blocks: list | None = None,
) -> Reply:
    content = [*(extra_blocks or []), {"type": "text", "text": text}]
    return Reply(
        200,
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-fake",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
    )


def error_reply(status: int, message: str = "nope", **headers: str) -> Reply:
    return Reply(status, {"error": {"message": message, "type": "error", "code": status}}, headers)


class FakeServer:
    def __init__(self) -> None:
        self.script: list[Reply] = []
        self.seen: list[Seen] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the name http.server looks for
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.seen.append(Seen(self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                reply = outer.script.pop(0) if outer.script else error_reply(500, "script ran out")
                time.sleep(reply.delay_s)
                payload = json.dumps(reply.body).encode()
                try:
                    self.send_response(reply.status)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    for key, value in reply.headers.items():
                        self.send_header(key.replace("_", "-"), value)
                    self.end_headers()
                    self.wfile.write(payload)
                except BrokenPipeError, ConnectionResetError:
                    pass  # the client gave up (a timeout test)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_port)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def reply(self, *replies: Reply) -> None:
        self.script.extend(replies)


@contextmanager
def fake_server() -> Iterator[FakeServer]:
    server = FakeServer()
    server.thread.start()
    try:
        yield server
    finally:
        server.server.shutdown()
        server.server.server_close()
