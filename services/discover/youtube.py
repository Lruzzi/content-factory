"""What yt-dlp says about a channel's streams and uploads. Read-only: nothing is downloaded here, and every command is
an argument list built from a link that was checked first."""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

CHANNEL = re.compile(r"https://(?:www\.)?youtube\.com/[^\s\x00-\x1f]+")
CHANNEL_PATH = re.compile(r"/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+)")
TAB = re.compile(r"/(?:live|streams|videos|featured)/?$")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
TIMEOUT_S = 180


class DiscoverError(RuntimeError):
    pass


@dataclass(frozen=True)
class Video:
    id: str
    title: str
    duration_s: float | None
    views: int
    live_status: str | None
    availability: str | None
    uploaded: datetime | None = None
    heatmap: tuple[tuple[float, float, float], ...] = ()  # (start s, end s, how much it was replayed)


def tab_urls(channel_url: str) -> list[str]:
    """The pages of a channel that list its ended streams and its uploads."""
    url = channel_url.strip()
    if not CHANNEL.fullmatch(url) or not CHANNEL_PATH.search(url):
        raise ValueError(f"not a link to a YouTube channel: {channel_url[:60]!r}")
    base = TAB.sub("", url.rstrip("/"))
    return [f"{base}/streams", f"{base}/videos"]


def flat_command(ytdlp: str, tab_url: str, limit: int) -> list[str]:
    if not CHANNEL.fullmatch(tab_url):
        raise ValueError(f"not a usable channel link: {tab_url[:60]!r}")
    return [ytdlp, "--flat-playlist", "--dump-single-json", "--no-warnings", "--playlist-end", str(limit), tab_url]


def info_command(ytdlp: str, video_id: str) -> list[str]:
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError(f"not a video id: {video_id[:30]!r}")
    return [
        ytdlp, "--dump-single-json", "--no-download", "--no-warnings", "--no-playlist",
        f"https://www.youtube.com/watch?v={video_id}",
    ]  # fmt: skip


def _int(value: Any) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _entries(doc: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for entry in doc.get("entries") or []:
        if isinstance(entry, dict):
            if entry.get("entries"):
                yield from _entries(entry)
            else:
                yield entry


def parse_flat(doc: dict[str, Any]) -> list[Video]:
    videos = []
    for entry in _entries(doc):
        video_id = entry.get("id")
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
            continue
        duration = entry.get("duration")
        videos.append(
            Video(
                id=video_id,
                title=str(entry.get("title") or ""),
                duration_s=float(duration) if isinstance(duration, int | float) else None,
                views=_int(entry.get("view_count")),
                live_status=entry.get("live_status"),
                availability=entry.get("availability"),
            )
        )
    return videos


def _uploaded(doc: dict[str, Any]) -> datetime | None:
    for key in ("release_timestamp", "timestamp"):
        if isinstance(doc.get(key), int | float):
            return datetime.fromtimestamp(doc[key], UTC)
    date = doc.get("upload_date")
    if isinstance(date, str) and re.fullmatch(r"\d{8}", date):
        return datetime(int(date[:4]), int(date[4:6]), int(date[6:]), tzinfo=UTC)
    return None


def parse_info(doc: dict[str, Any]) -> Video:
    duration = doc.get("duration")
    heat = tuple(
        (float(b["start_time"]), float(b["end_time"]), float(b["value"]))
        for b in doc.get("heatmap") or []
        if isinstance(b, dict) and all(isinstance(b.get(k), int | float) for k in ("start_time", "end_time", "value"))
    )
    return Video(
        id=str(doc.get("id") or ""),
        title=str(doc.get("title") or ""),
        duration_s=float(duration) if isinstance(duration, int | float) else None,
        views=_int(doc.get("view_count")),
        live_status=doc.get("live_status"),
        availability=doc.get("availability"),
        uploaded=_uploaded(doc),
        heatmap=heat,
    )


def _ytdlp() -> str:
    return shutil.which("yt-dlp") or "yt-dlp"


def _run(command: list[str]) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)  # noqa: S603  # nosec B603
    except FileNotFoundError:
        raise DiscoverError("yt-dlp is not installed") from None
    except subprocess.TimeoutExpired:
        raise DiscoverError("yt-dlp timed out") from None
    if done.returncode != 0:
        lines = done.stderr.strip().splitlines()
        raise DiscoverError(lines[-1][:200] if lines else f"yt-dlp exited with {done.returncode}")
    return done.stdout


def _json(text: str) -> dict[str, Any]:
    try:
        doc = json.loads(text)
    except ValueError:
        raise DiscoverError("yt-dlp did not answer with JSON") from None
    if not isinstance(doc, dict):
        raise DiscoverError("yt-dlp did not answer with a JSON object")
    return doc


def list_videos(channel_url: str, *, limit: int = 40, run: Callable[[list[str]], str] = _run) -> list[Video]:
    """The recent ended streams and uploads of a channel, each once. A channel without a streams tab still lists its
    videos."""
    videos: dict[str, Video] = {}
    problems: list[str] = []
    for tab in tab_urls(channel_url):
        try:
            found = parse_flat(_json(run(flat_command(_ytdlp(), tab, limit))))
        except DiscoverError as exc:
            problems.append(str(exc))
            continue
        for video in found:
            videos.setdefault(video.id, video)
    if not videos and problems and len(problems) == 2:
        raise DiscoverError(problems[0])
    return list(videos.values())


def video_info(video_id: str, *, run: Callable[[list[str]], str] = _run) -> Video:
    return parse_info(_json(run(info_command(_ytdlp(), video_id))))
