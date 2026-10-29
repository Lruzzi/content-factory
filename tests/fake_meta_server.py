"""A tiny stand-in for the Meta Graph API: Facebook Page Reels, Instagram Reels containers, and the rupload host."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

V = "v26.0"


@dataclass
class Call:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    form: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass
class FakeMeta:
    calls: list[Call] = field(default_factory=list)
    base: str = ""
    errors: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)  # "METHOD path-regex" -> reply
    upload_failures: int = 0  # the next N uploads answer 503 and keep nothing
    fb_received: dict[str, bytearray] = field(default_factory=dict)
    fb_published: set[str] = field(default_factory=set)
    fb_finish: list[dict[str, str]] = field(default_factory=list)
    ig_received: dict[str, bytearray] = field(default_factory=dict)
    ig_containers: dict[str, dict[str, Any]] = field(default_factory=dict)
    ig_created: list[dict[str, str]] = field(default_factory=list)
    ig_media: list[dict[str, str]] = field(default_factory=list)
    polls_until_finished: int = 0  # container status answers IN_PROGRESS this many times first
    file_size: int = 0

    def calls_to(self, method: str, pattern: str) -> list[Call]:
        return [c for c in self.calls if c.method == method and re.fullmatch(pattern, c.path)]

    def start(self) -> ThreadingHTTPServer:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _call(self, method: str) -> Call:
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

            def _scripted_error(self, call: Call) -> bool:
                for key, (status, payload) in outer.errors.items():
                    method, pattern = key.split(" ", 1)
                    if method == call.method and re.fullmatch(pattern, call.path):
                        self._json(status, payload)
                        return True
                return False

            def do_POST(self) -> None:  # noqa: N802 - the name http.server looks for
                call = self._call("POST")
                if self._scripted_error(call):
                    return
                p = call.path
                if m := re.fullmatch(rf"/{V}/(\w+)/video_reels", p):
                    return self._fb_reels(call, m.group(1))
                if m := re.fullmatch(rf"/video-upload/{V}/(\w+)", p):
                    return self._upload(call, outer.fb_received, m.group(1))
                if m := re.fullmatch(rf"/ig-api-upload/{V}/(\w+)", p):
                    return self._upload(call, outer.ig_received, m.group(1))
                if m := re.fullmatch(rf"/{V}/(\w+)/media", p):
                    return self._ig_container(call, m.group(1))
                if m := re.fullmatch(rf"/{V}/(\w+)/media_publish", p):
                    return self._ig_publish(call)
                self._json(404, {"error": {"message": "unknown path", "code": 100}})

            def do_GET(self) -> None:  # noqa: N802
                call = self._call("GET")
                if self._scripted_error(call):
                    return
                p, fields = call.path, call.query.get("fields", "")
                if m := re.fullmatch(rf"/{V}/(\w+)/media", p):
                    return self._json(200, {"data": list(reversed(outer.ig_media))})
                if m := re.fullmatch(rf"/{V}/(\w+)", p):
                    return self._node(m.group(1), fields)
                self._json(404, {"error": {"message": "unknown path", "code": 100}})

            def _fb_reels(self, call: Call, page: str) -> None:
                phase = call.form.get("upload_phase") or call.query.get("upload_phase")
                if phase == "start":
                    video = f"fbv{len(outer.fb_received) + 1}"
                    outer.fb_received[video] = bytearray()
                    return self._json(200, {"video_id": video, "upload_url": f"{outer.base}/video-upload/{video}"})
                fields = call.form or call.query
                outer.fb_finish.append(fields)
                outer.fb_published.add(fields["video_id"])
                self._json(200, {"success": True})

            def _upload(self, call: Call, store: dict[str, bytearray], key: str) -> None:
                if key not in store:
                    return self._json(404, {"error": {"message": "no such upload", "code": 100}})
                if outer.upload_failures > 0:
                    outer.upload_failures -= 1
                    return self._json(503, {"error": {"message": "try later", "code": 2}})
                if int(call.headers["offset"]) != len(store[key]):
                    return self._json(400, {"error": {"message": "OffsetInvalidError", "code": 1363037}})
                store[key].extend(call.body)
                outer.file_size = int(call.headers["file_size"])
                if key in outer.ig_containers and len(store[key]) >= outer.file_size:
                    outer.ig_containers[key]["uploaded"] = True
                self._json(200, {"success": True})

            def _ig_container(self, call: Call, ig: str) -> None:
                outer.ig_created.append(call.form)
                cid = f"igc{len(outer.ig_containers) + 1}"
                # a container made from a link is filled by Instagram itself: nothing is uploaded to it
                outer.ig_containers[cid] = {
                    "status_code": "IN_PROGRESS",
                    "polls": 0,
                    "uploaded": "video_url" in call.form,
                }
                outer.ig_received[cid] = bytearray()
                self._json(200, {"id": cid, "uri": f"{outer.base}/ig-api-upload/{V}/{cid}"})

            def _ig_publish(self, call: Call) -> None:
                cid = call.form["creation_id"]
                container = outer.ig_containers.get(cid)
                if container is None or container["status_code"] != "FINISHED":
                    return self._json(
                        400, {"error": {"message": "Media ID is not available", "code": 9007, "error_subcode": 2207027}}
                    )
                container["status_code"] = "PUBLISHED"
                media = f"igm{len(outer.ig_media) + 1}"
                outer.ig_media.append(
                    {
                        "id": media,
                        "permalink": f"https://www.instagram.com/reel/{media}/",
                        "timestamp": "2099-01-01T00:00:00+0000",
                    }
                )
                self._json(200, {"id": media})

            def _node(self, node: str, fields: str) -> None:
                if node in outer.fb_received:  # a Facebook video
                    if "permalink_url" in fields:
                        return self._json(200, {"permalink_url": f"/reel/{node}"})
                    done = len(outer.fb_received[node]) >= outer.file_size > 0
                    published = node in outer.fb_published
                    moved = len(outer.fb_received[node])
                    video_status = "ready" if published else ("upload_complete" if done else "uploading")
                    status = {
                        "video_status": video_status,
                        "uploading_phase": {"status": "complete" if done else "in_progress", "bytes_transfered": moved},
                        "publishing_phase": {
                            "status": "complete" if published else "not_started",
                            "publish_status": "published" if published else "draft",
                        },
                    }
                    return self._json(200, {"status": status, "id": node})
                if node in outer.ig_containers:
                    c = outer.ig_containers[node]
                    if c["status_code"] == "IN_PROGRESS" and c["uploaded"]:
                        c["polls"] += 1
                        if c["polls"] > outer.polls_until_finished:
                            c["status_code"] = "FINISHED"
                    return self._json(200, {"status_code": c["status_code"], "id": node})
                media = next((m for m in outer.ig_media if m["id"] == node), None)
                if media is not None:
                    return self._json(200, {"permalink": media["permalink"], "id": node})
                self._json(404, {"error": {"message": "Unsupported get request", "code": 100}})

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
def fake_meta() -> Iterator[tuple[FakeMeta, str]]:
    fake = FakeMeta()
    server = fake.start()
    fake.base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield fake, fake.base
    finally:
        server.shutdown()
        server.server_close()
