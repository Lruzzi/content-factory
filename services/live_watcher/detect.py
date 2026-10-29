"""Ask yt-dlp whether a channel is live. Free, unlike the API's live search (100 quota units a call)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

CHANNEL = re.compile(r"https://(?:www\.)?youtube\.com/[^\s\x00-\x1f]+")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{6,20}")
RECORDABLE = {"public", "unlisted"}  # members-only, private and the rest are never recorded
NOT_LIVE = "is not currently live"
UPCOMING = "live event will begin"  # yt-dlp reports a scheduled stream about to start as an error
ERROR_ID = re.compile(r"\[youtube\] ([A-Za-z0-9_-]{6,20}):")


class DetectError(RuntimeError):
    pass


@dataclass(frozen=True)
class Detection:
    state: str  # live | upcoming | skipped
    video_id: str
    url: str
    title: str
    reason: str  # why a stream was skipped


def command(ytdlp: str, channel_url: str) -> list[str]:
    if not CHANNEL.fullmatch(channel_url):
        raise ValueError(f"not a usable youtube url: {channel_url!r}")
    return [ytdlp, "--dump-single-json", "--no-download", "--no-warnings", "--playlist-items", "1",
            channel_url.rstrip("/") + "/live"]  # fmt: skip


def classify(info: dict[str, Any]) -> Detection | None:
    status = info.get("live_status")
    if status not in ("is_live", "is_upcoming"):
        return None
    video_id = str(info.get("id") or "")
    if not VIDEO_ID.fullmatch(video_id):
        raise DetectError(f"yt-dlp answer has no usable video id: {video_id!r}")
    url = f"https://www.youtube.com/watch?v={video_id}"
    title = str(info.get("title") or "")
    availability = str(info.get("availability") or "unknown")
    if availability not in RECORDABLE:
        return Detection("skipped", video_id, url, title, availability)
    return Detection("live" if status == "is_live" else "upcoming", video_id, url, title, "")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)  # noqa: S603  # nosec B603
    except subprocess.TimeoutExpired as exc:
        raise DetectError("yt-dlp timed out") from exc


def check(channel_url: str, run: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run) -> Detection | None:
    ytdlp = shutil.which("yt-dlp")
    if ytdlp is None:
        raise DetectError("yt-dlp is not installed")
    done = run(command(ytdlp, channel_url))
    if done.returncode != 0:
        if NOT_LIVE in done.stderr:
            return None
        if UPCOMING in done.stderr:  # not live yet, not a failure: the next check will find it live
            found = ERROR_ID.search(done.stderr)
            if found is None:
                return None
            return Detection("upcoming", found.group(1), f"https://www.youtube.com/watch?v={found.group(1)}", "", "")
        raise DetectError(f"yt-dlp failed: {done.stderr.strip()[-300:]}")
    try:
        info = json.loads(done.stdout)
    except ValueError as exc:
        raise DetectError("yt-dlp did not return JSON") from exc
    return classify(info if isinstance(info, dict) else {})


def is_live(video_id: str, run: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run) -> bool:
    """Whether one video is live right now. A video that cannot be checked is an error, never a guess."""
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError(f"not a usable video id: {video_id!r}")
    ytdlp = shutil.which("yt-dlp")
    if ytdlp is None:
        raise DetectError("yt-dlp is not installed")
    cmd = command(ytdlp, f"https://www.youtube.com/watch?v={video_id}")
    cmd[-1] = cmd[-1].removesuffix("/live")
    done = run(cmd)
    if done.returncode != 0:
        raise DetectError(f"yt-dlp failed: {done.stderr.strip()[-300:]}")
    try:
        info = json.loads(done.stdout)
    except ValueError as exc:
        raise DetectError("yt-dlp did not return JSON") from exc
    return isinstance(info, dict) and info.get("live_status") == "is_live"
