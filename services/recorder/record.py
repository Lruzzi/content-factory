"""Record one live stream into fixed-length segments: Streamlink writes the stream to a pipe, FFmpeg copies it into
numbered .ts files and prints each one as it closes. Argument lists only; the URL is checked first."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess  # nosec B404
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from common.media import MediaStore

log = logging.getLogger(__name__)
URL = re.compile(r"(?:hls(?:variant)?://)?https?://[^\s\x00-\x1f]+")


class RecorderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Segment:
    index: int
    key: str
    start_s: float  # seconds since the recording began
    end_s: float


def streamlink_command(streamlink: str, url: str, quality: str, min_quality: str | None = None) -> list[str]:
    excludes = f">{quality},>{quality}60" + (f",<{min_quality}" if min_quality else "")
    return [streamlink, "--stdout", "--stream-sorting-excludes", excludes, url, "best"]


def ffmpeg_command(ffmpeg: str, directory: Path, segment_s: int, start_index: int = 0) -> list[str]:
    return [
        ffmpeg, "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe", "-i", "pipe:0",
        "-c", "copy", "-f", "segment", "-segment_time", str(segment_s), "-reset_timestamps", "1",
        "-segment_format", "mpegts", "-segment_start_number", str(start_index),
        "-segment_list", "pipe:1", "-segment_list_type", "csv",
        str(directory / "%05d.ts"),
    ]  # fmt: skip


def _binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RecorderError(f"{name} is not installed")
    return path


def _tail(handle: IO[bytes]) -> str:
    handle.seek(0)
    return "\n".join(handle.read().decode("utf-8", "replace").strip().splitlines()[-6:])


def record(
    url: str,
    store: MediaStore,
    stream_id: str,
    *,
    segment_s: int,
    on_segment: Callable[[Segment], None],
    quality: str = "720p",
    min_quality: str | None = None,
    stop: threading.Event | None = None,
    start_index: int = 0,
    start_s: float = 0.0,
) -> str:
    """Record until the stream ends or `stop` is set; call `on_segment` for each finished segment.
    A restart continues at `start_index` and `start_s`.
    Returns "ended" or "stopped". Raises RecorderError if the stream cannot be read or the callback fails."""
    if not URL.fullmatch(url):
        raise ValueError(f"not a usable stream url: {url!r}")
    stop = stop or threading.Event()
    streamlink, ffmpeg = _binary("streamlink"), _binary("ffmpeg")
    first = store.path(f"segments/{stream_id}/{start_index:05d}.ts")  # type: ignore[attr-defined]
    first.parent.mkdir(parents=True, exist_ok=True)

    failure: list[str] = []
    done = threading.Event()
    with tempfile.TemporaryFile() as link_log, tempfile.TemporaryFile() as ff_log:
        link = subprocess.Popen(  # noqa: S603  # nosec B603
            streamlink_command(streamlink, url, quality, min_quality), stdout=subprocess.PIPE, stderr=link_log
        )
        cut = subprocess.Popen(  # noqa: S603  # nosec B603
            ffmpeg_command(ffmpeg, first.parent, segment_s, start_index),
            stdin=link.stdout, stdout=subprocess.PIPE, stderr=ff_log,
        )  # fmt: skip
        assert link.stdout is not None and cut.stdout is not None  # noqa: S101  # nosec B101
        link.stdout.close()  # FFmpeg owns the read end now

        def watch() -> None:
            while not done.is_set():
                if stop.wait(0.2):
                    link.terminate()  # FFmpeg then sees end of input and closes the last segment
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            for raw in cut.stdout:
                name, start, end = raw.decode().strip().split(",")
                index = int(Path(name).stem)
                try:
                    on_segment(
                        Segment(
                            index, f"segments/{stream_id}/{index:05d}.ts", start_s + float(start), start_s + float(end)
                        )
                    )
                except Exception as exc:
                    failure.append(f"segment callback failed: {exc}")
                    stop.set()
                    break
        finally:
            done.set()
            if link.poll() is None:
                link.terminate()
            for process in (link, cut):
                try:
                    process.wait(15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            cut.stdout.close()
        if failure:
            raise RecorderError(failure[0])
        if stop.is_set():
            return "stopped"
        if link.returncode != 0:
            raise RecorderError(f"streamlink exited with {link.returncode}: {_tail(link_log)}")
        if cut.returncode != 0:
            raise RecorderError(f"ffmpeg exited with {cut.returncode}: {_tail(ff_log)}")
        log.warning("streamlink finished by itself; its last lines: %s", _tail(link_log))
        return "ended"
