"""A small Meta Graph API client for Facebook Page Reels and Instagram Reels (resumable upload).

The Page token goes in an `Authorization: OAuth` header, never in an address, so it cannot land in a log or an error.
Calls that create something (start a Reel, finish it, create a container, publish) are never repeated automatically:
a repeat could post twice. The caller keeps its own record of what happened (the `posts` row) and checks the platform's
status before trying again.

Protocol facts come from Meta's documentation as of Oct 2026 (Reels Publishing API, Instagram Content Publishing):
check them when something fails, Meta changes these often."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx2

for _noisy in ("httpx2", "httpcore2"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com"
VERSION = "v26.0"
ID = re.compile(r"[A-Za-z0-9_]+")
RETRYABLE_CODES = {1, 2, 341}  # temporary trouble: the same request again in a moment
LIMIT_CODES = {4, 17, 32, 368, 613}  # rate limits and "we limit how often you can post": hours, not a moment
TOKEN_CODES = {102, 190}  # the token is invalid, expired or revoked


class MetaError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, code: int | None = None) -> None:
        super().__init__(message)
        self.retryable, self.code = retryable, code


class MetaLimit(MetaError):
    """Meta says this account or app has done enough for now. Trying again in a moment only makes it worse."""


class MetaTokenError(MetaError):
    """The saved token no longer works: it has to be made again (make meta-tokens)."""


@dataclass(frozen=True)
class FbStatus:
    video_status: str
    upload_complete: bool
    bytes_transferred: int
    published: bool


def _id(value: str) -> str:
    if not ID.fullmatch(value or ""):
        raise ValueError(f"not a usable Meta id: {value!r}")
    return value


class Meta:
    def __init__(
        self,
        token: str,
        *,
        graph_url: str = GRAPH,
        rupload_url: str = RUPLOAD,
        version: str = VERSION,
        timeout_s: float = 120,
        retry_wait_s: float = 3,
        upload_attempts: int = 3,
    ) -> None:
        self._auth = {"Authorization": f"OAuth {token}"}
        self._graph, self._rupload, self._v = graph_url.rstrip("/"), rupload_url.rstrip("/"), version
        self._wait, self._upload_attempts = retry_wait_s, upload_attempts
        self._http = httpx2.Client(timeout=timeout_s)

    # ---------- plumbing ----------
    def _what_came_back(self, reply: httpx2.Response) -> str:
        """A short piece of an answer that is not a Graph error, so the operator can tell why (the upload host
        answers in its own shape, `debug_info`). Meta never echoes the token; the length is capped all the same."""
        token = self._auth["Authorization"].removeprefix("OAuth ")
        try:
            body = reply.json()
        except ValueError:
            shown = " ".join(reply.text.split()) or "no body"
        else:
            info = body.get("debug_info") if isinstance(body, dict) else None
            if isinstance(info, dict):
                shown = " ".join(f"{info[k]}" for k in ("type", "message") if info.get(k)) or "no detail"
            else:
                shown = json.dumps(body)
        return shown.replace(token, "[token]")[:200]

    def _fail(self, reply: httpx2.Response) -> MetaError:
        try:
            error = reply.json()["error"]
            message, code = str(error.get("message", "")), error.get("code")
        except ValueError, KeyError, TypeError, AttributeError:
            message, code = f"HTTP {reply.status_code}: {self._what_came_back(reply)}", None
        text = f"Meta refused the request: {message} (code {code})"
        if code in TOKEN_CODES:
            return MetaTokenError(text, code=code)
        if code in LIMIT_CODES:
            return MetaLimit(text, code=code)
        return MetaError(text, retryable=reply.status_code >= 500 or code in RETRYABLE_CODES, code=code)

    def _send(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        try:
            reply = self._http.request(method, url, headers=self._auth | kw.pop("headers", {}), **kw)
        except httpx2.HTTPError as exc:
            raise MetaError(f"could not reach Meta ({type(exc).__name__})", retryable=True) from None
        if reply.status_code >= 400:
            raise self._fail(reply)
        try:
            body = reply.json()
        except ValueError:
            raise MetaError(f"Meta answered with something that is not JSON (HTTP {reply.status_code})") from None
        return body if isinstance(body, dict) else {}

    def _get(self, node: str, edge: str = "", **params: str) -> dict[str, Any]:
        path = f"{_id(node)}/{edge}" if edge else _id(node)
        return self._send("GET", f"{self._graph}/{self._v}/{path}", params=params)

    def _post(self, node: str, **data: str) -> dict[str, Any]:
        return self._send("POST", f"{self._graph}/{self._v}/{node}", data=data)

    def _upload(self, url: str, file: Path, offset: int) -> None:
        size = file.stat().st_size
        with file.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
        for attempt in range(1, self._upload_attempts + 1):
            try:
                body = self._send("POST", url, content=data, headers={"offset": str(offset), "file_size": str(size)})
            except MetaError as exc:
                if not exc.retryable or attempt == self._upload_attempts:
                    raise
                time.sleep(self._wait)  # the same bytes at the same offset: safe to send again
                continue
            if body.get("success") is False:
                raise MetaError("Meta did not accept the upload")
            return

    # ---------- Facebook Page Reels ----------
    def fb_reel_start(self, page_id: str) -> str:
        return str(self._post(f"{_id(page_id)}/video_reels", upload_phase="start")["video_id"])

    def fb_upload(self, video_id: str, file: Path, *, offset: int) -> None:
        self._upload(f"{self._rupload}/video-upload/{self._v}/{_id(video_id)}", file, offset)

    def fb_reel_finish(self, page_id: str, video_id: str, *, description: str, title: str) -> None:
        self._post(
            f"{_id(page_id)}/video_reels",
            upload_phase="finish",
            video_id=_id(video_id),
            video_state="PUBLISHED",
            description=description,
            title=title,
        )

    def fb_status(self, video_id: str) -> FbStatus:
        status = self._get(video_id, fields="status").get("status", {})
        uploading = status.get("uploading_phase", {})
        state = str(status.get("video_status", ""))
        moved = uploading.get("bytes_transfered", uploading.get("bytes_transferred", 0))  # Meta's spelling
        return FbStatus(
            video_status=state,
            upload_complete=uploading.get("status") in ("complete", "completed")
            or state in ("upload_complete", "processing", "ready"),
            bytes_transferred=int(moved or 0),
            published=status.get("publishing_phase", {}).get("publish_status") == "published",
        )

    def fb_permalink(self, video_id: str) -> str | None:
        link = self._get(video_id, fields="permalink_url").get("permalink_url")
        if not link:
            return None
        return link if str(link).startswith("http") else f"https://www.facebook.com{link}"

    # ---------- Instagram Reels ----------
    def ig_create_container(self, ig_user_id: str, *, caption: str) -> tuple[str, str]:
        """A Reel container set up for a resumable upload. Returns (container id, address to send the bytes to)."""
        body = self._post(
            f"{_id(ig_user_id)}/media",
            media_type="REELS",
            upload_type="resumable",
            caption=caption,
            share_to_feed="true",
        )
        return str(body["id"]), str(body["uri"])

    def ig_create_container_from_url(self, ig_user_id: str, *, caption: str, video_url: str) -> str:
        """A Reel container that Instagram fills by fetching `video_url` itself, in the background. Unlike the resumable
        upload there is no single request that has to finish inside Instagram's own deadline (about 48 s), which our
        1080p60 clips of 50 s and more kept missing ('ProcessingFailedError')."""
        parts = urlparse(video_url)
        local = parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")  # tests only
        if not parts.netloc or not (parts.scheme == "https" or local):
            raise ValueError("the video link must be an https address")
        body = self._post(
            f"{_id(ig_user_id)}/media", media_type="REELS", video_url=video_url, caption=caption, share_to_feed="true"
        )
        return str(body["id"])

    def ig_upload(self, uri: str, file: Path) -> None:
        if urlparse(uri).netloc != urlparse(self._rupload).netloc:
            raise MetaError("Meta gave an upload address on another host; not following it")
        self._upload(uri, file, 0)

    def ig_status(self, container_id: str) -> str:
        return str(self._get(container_id, fields="status_code").get("status_code", ""))

    def ig_publish(self, ig_user_id: str, container_id: str) -> str:
        return str(self._post(f"{_id(ig_user_id)}/media_publish", creation_id=_id(container_id))["id"])

    def ig_permalink(self, media_id: str) -> str | None:
        return self._get(media_id, fields="permalink").get("permalink")

    def ig_recent_media(self, ig_user_id: str, limit: int = 5) -> list[dict[str, Any]]:
        body = self._get(ig_user_id, "media", fields="id,permalink,timestamp", limit=str(limit))
        return list(body.get("data", []))
