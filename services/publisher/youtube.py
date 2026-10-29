"""Upload a clip to YouTube with the resumable upload protocol (videos.insert), and read back what YouTube did with it.

The upload is sent in chunks. If a chunk fails for a moment, the client asks YouTube how much it has and carries on from
there, so nothing is sent twice. The access token comes from a callable (see google_oauth.AccessTokens) and is never
logged or put in an error. A daily quota error is not retried: it ends the job so you are told.

Costs and limits to know (from memory of the docs, check them): a `videos.insert` costs 1600 units of the 10,000 daily
quota, so about six uploads a day. A project that has not passed YouTube's API audit may have its uploads locked to
private: `status()` shows what YouTube actually did."""

from __future__ import annotations

import logging
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx2

from services.publisher.google_oauth import AUTHORIZE_HINT, AuthExpired

for _noisy in ("httpx2", "httpcore2"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

API = "https://www.googleapis.com"
PRIVACY = ("private", "unlisted", "public")
TITLE_MAX = 100
DESCRIPTION_MAX_BYTES = 4900  # YouTube allows 5000 bytes
CHUNK_UNIT = 256 * 1024  # chunks must be a multiple of this, except the last


class YouTubeError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class UploadLimit(YouTubeError):
    """The channel has uploaded as much as YouTube allows for now (or the API quota is used up): try again later."""


LIMIT_REASONS = {
    "uploadLimitExceeded",
    "quotaExceeded",
    "dailyLimitExceeded",
    "rateLimitExceeded",
    "userRateLimitExceeded",
}


class SessionGone(YouTubeError):
    """YouTube no longer knows the upload session, so we cannot tell whether the video was posted."""


@dataclass(frozen=True)
class Uploaded:
    video_id: str
    url: str


def _text(text: str) -> str:
    kept = "".join(ch for ch in text if ch in "\n " or unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"))
    return kept.replace("<", "‹").replace(">", "›").strip()


def _title(text: str) -> str:
    one_line = " ".join(_text(text).split())
    return one_line if len(one_line) <= TITLE_MAX else one_line[: TITLE_MAX - 1].rsplit(" ", 1)[0] + "…"


def _description(text: str) -> str:
    out = _text(text)
    while len(out.encode()) > DESCRIPTION_MAX_BYTES:
        out = out[: int(len(out) * 0.9)]
    return out


def _reason(reply: httpx2.Response) -> str:
    try:
        body = reply.json()
        return str(body["error"]["errors"][0]["reason"])
    except ValueError, KeyError, IndexError, TypeError:
        return f"HTTP {reply.status_code}"


class YouTube:
    def __init__(
        self,
        access_token: Callable[[], str],
        *,
        base_url: str = API,
        chunk_bytes: int = 8 * 1024 * 1024,
        retry_wait_s: float = 2,
        max_attempts: int = 5,
        timeout_s: float = 120,
    ) -> None:
        if chunk_bytes % CHUNK_UNIT:
            raise ValueError("chunk_bytes must be a multiple of 256 KiB")
        self._token, self._base = access_token, base_url.rstrip("/")
        self._chunk, self._wait, self._attempts = chunk_bytes, retry_wait_s, max_attempts
        self._http = httpx2.Client(timeout=timeout_s)

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def _check(self, reply: httpx2.Response, what: str) -> None:
        if reply.status_code == 401:
            raise AuthExpired(f"YouTube rejected the access token: {AUTHORIZE_HINT}")
        if reply.status_code >= 400:
            reason = _reason(reply)
            if reason in LIMIT_REASONS:
                raise UploadLimit(f"YouTube refused {what}: {reason}")
            raise YouTubeError(f"YouTube refused {what}: {reason}", retryable=reply.status_code >= 500)

    def upload(
        self,
        file: Path,
        *,
        title: str,
        description: str,
        privacy: str,
        category_id: str = "20",
        session_url: str | None = None,
        on_session: Callable[[str], None] | None = None,
    ) -> Uploaded:
        """Upload `file`. `on_session` is told the upload address before any bytes go, so the caller can save it; given
        that address back as `session_url`, the upload carries on where it stopped (or is recognised as finished)."""
        if privacy not in PRIVACY:
            raise ValueError(f"privacy must be one of {', '.join(PRIVACY)}")
        if not file.is_file():
            raise YouTubeError(f"the video file {file.name!r} does not exist")
        size = file.stat().st_size
        if size == 0:
            raise YouTubeError(f"the video file {file.name!r} is empty")
        if session_url is not None:
            self._same_host(session_url)
            return self._send(session_url, file, size, resume=True)
        metadata = {
            "snippet": {"title": _title(title), "description": _description(description), "categoryId": category_id},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }
        session = self._start(metadata, size)
        if on_session is not None:
            on_session(session)
        return self._send(session, file, size)

    def _start(self, metadata: dict[str, Any], size: int) -> str:
        try:
            reply = self._http.post(
                f"{self._base}/upload/youtube/v3/videos",
                params={"uploadType": "resumable", "part": "snippet,status"},
                json=metadata,
                headers=self._auth() | {"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
            )
        except httpx2.HTTPError as exc:
            raise YouTubeError(f"could not reach YouTube ({type(exc).__name__})", retryable=True) from None
        self._check(reply, "to start the upload")
        session = reply.headers.get("Location", "")
        self._same_host(session)
        return session

    def _same_host(self, session: str) -> None:
        if urlparse(session).netloc != urlparse(self._base).netloc:
            raise YouTubeError("an upload address on another host; not following it")

    def _put(self, session: str, data: bytes, content_range: str) -> httpx2.Response | None:
        try:
            return self._http.put(session, content=data, headers=self._auth() | {"Content-Range": content_range})
        except httpx2.HTTPError:
            return None  # a network failure: the caller asks how far YouTube got

    @staticmethod
    def _received(reply: httpx2.Response) -> int:
        """How many bytes YouTube holds, from a 308 answer's Range header."""
        header = reply.headers.get("Range", "")
        return int(header.rsplit("-", 1)[1]) + 1 if "-" in header else 0

    def _send(self, session: str, file: Path, size: int, *, resume: bool = False) -> Uploaded:
        offset, failures = 0, 0
        if resume:  # a saved session: ask how far YouTube got before sending anything
            asked = self._put(session, b"", f"bytes */{size}")
            if asked is None:
                raise YouTubeError("could not reach YouTube to resume the upload", retryable=True)
            if asked.status_code in (200, 201):
                return self._done(asked)
            if asked.status_code in (404, 410):
                raise SessionGone("YouTube no longer knows this upload, so it cannot be checked whether it was posted")
            if asked.status_code != 308:
                self._check(asked, "the resume request")
                raise YouTubeError(f"unexpected answer to the resume request: HTTP {asked.status_code}", retryable=True)
            offset = self._received(asked)
        with file.open("rb") as handle:
            while True:
                end = min(offset + self._chunk, size)
                handle.seek(offset)
                reply = self._put(session, handle.read(end - offset), f"bytes {offset}-{end - 1}/{size}")
                if reply is not None and reply.status_code in (200, 201):
                    return self._done(reply)
                if reply is not None and reply.status_code == 308:
                    offset, failures = self._received(reply), 0
                    continue
                if reply is not None and reply.status_code < 500:
                    self._check(reply, "the upload")
                failures += 1
                if failures >= self._attempts:
                    raise YouTubeError("YouTube kept failing during the upload; try again later", retryable=True)
                time.sleep(self._wait)
                asked = self._put(session, b"", f"bytes */{size}")  # where did YouTube get to?
                if asked is not None and asked.status_code in (200, 201):
                    return self._done(asked)
                if asked is not None and asked.status_code == 308:
                    offset = self._received(asked)

    @staticmethod
    def _done(reply: httpx2.Response) -> Uploaded:
        try:
            video_id = str(reply.json()["id"])
        except ValueError, KeyError, TypeError:
            raise YouTubeError("YouTube finished the upload but did not say which video it is") from None
        return Uploaded(video_id, f"https://www.youtube.com/watch?v={video_id}")

    def channel(self) -> dict[str, str]:
        """The YouTube channel this sign-in belongs to (needs the readonly permission)."""
        try:
            reply = self._http.get(
                f"{self._base}/youtube/v3/channels", params={"part": "snippet", "mine": "true"}, headers=self._auth()
            )
        except httpx2.HTTPError as exc:
            raise YouTubeError(f"could not reach YouTube ({type(exc).__name__})", retryable=True) from None
        self._check(reply, "the channel request")
        items = reply.json().get("items", [])
        if not items:
            raise YouTubeError("this Google account has no YouTube channel: create one first")
        return {"id": str(items[0]["id"]), "title": str(items[0]["snippet"]["title"])}

    def status(self, video_id: str) -> dict[str, str] | None:
        """Privacy and processing state of one of our videos, or None if YouTube does not know it."""
        try:
            reply = self._http.get(
                f"{self._base}/youtube/v3/videos", params={"part": "status", "id": video_id}, headers=self._auth()
            )
        except httpx2.HTTPError as exc:
            raise YouTubeError(f"could not reach YouTube ({type(exc).__name__})", retryable=True) from None
        self._check(reply, "the status request")
        items = reply.json().get("items", [])
        if not items:
            return None
        status = items[0].get("status", {})
        return {"privacy": str(status.get("privacyStatus")), "upload": str(status.get("uploadStatus"))}
