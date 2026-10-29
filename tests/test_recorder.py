"""Recording a live stream into segments (FR-15). The stream is a real live HLS feed made by FFmpeg and served locally,
so Streamlink and FFmpeg run for real without needing YouTube."""

from __future__ import annotations

import functools
import http.server
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from common.media import LocalStore
from services.recorder import record as rec
from services.render import ffmpeg as ff

needs_tools = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe") and shutil.which("streamlink")),
    reason="needs ffmpeg, ffprobe and streamlink",
)


# ---------- the commands (pure) ---------------------------------------------------------------------------------
def test_streamlink_writes_the_stream_to_stdout_and_never_takes_more_than_the_configured_quality():
    cmd = rec.streamlink_command("/usr/bin/streamlink", "https://www.youtube.com/watch?v=abc", "720p")
    assert cmd[:2] == ["/usr/bin/streamlink", "--stdout"]
    # YouTube names its streams 720p60, 1080p60 ...: "720p,best" would silently fall back to the best one (1080p60)
    assert cmd[cmd.index("--stream-sorting-excludes") + 1] == ">720p,>720p60"
    assert cmd[-2:] == ["https://www.youtube.com/watch?v=abc", "best"]


def test_streamlink_refuses_a_stream_below_the_minimum_quality_and_a_plain_command_has_no_minimum():
    url = "https://www.youtube.com/watch?v=abc"
    cmd = rec.streamlink_command("/usr/bin/streamlink", url, "1080p", "720p")
    # "<720p" removes 480p and 360p (it does not remove 720p60, which is above 720p); with nothing left Streamlink
    # stops with "the specified stream(s) 'best' could not be found", and the recorder leaves the hours alone
    assert cmd[cmd.index("--stream-sorting-excludes") + 1] == ">1080p,>1080p60,<720p"
    plain = rec.streamlink_command("/usr/bin/streamlink", url, "1080p")
    assert plain[plain.index("--stream-sorting-excludes") + 1] == ">1080p,>1080p60"


def test_ffmpeg_copies_the_stream_into_numbered_segments_and_lists_each_one_as_it_closes():
    cmd = rec.ffmpeg_command("/usr/bin/ffmpeg", Path("/media/segments/s1"), 600)
    assert cmd[cmd.index("-c") + 1] == "copy"  # no re-encoding while recording
    assert cmd[cmd.index("-segment_time") + 1] == "600"
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "file,pipe"
    assert cmd[cmd.index("-segment_list") + 1] == "pipe:1"
    assert cmd[-1] == "/media/segments/s1/%05d.ts"


@pytest.mark.parametrize(
    "bad",
    [
        "--help",
        "-o /tmp/x",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "javascript:1",
        "",
        "https://a b",
        "https://a\nb",
    ],
)
def test_urls_that_could_be_options_or_other_protocols_are_refused_before_anything_starts(bad, tmp_path):
    with pytest.raises(ValueError, match="url"):
        rec.record(bad, LocalStore(tmp_path), "s1", segment_s=4, on_segment=lambda s: None)


# ---------- real recordings -----------------------------------------------------------------------------------------
class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class LiveSource:
    """A live HLS feed produced by FFmpeg in real time and served over local HTTP."""

    def __init__(self, directory: Path, seconds: int):
        self.dir, self.seconds = directory, seconds
        self.ffmpeg: subprocess.Popen | None = None
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(directory))
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"hls://http://127.0.0.1:{self.server.server_port}/live.m3u8"

    def start(self) -> None:
        self.ffmpeg = subprocess.Popen(  # noqa: S603  # nosec B603
            [
                shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-nostdin", "-re",
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                "-t", str(self.seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-force_key_frames", "expr:gte(t,n_forced*2)", "-c:a", "aac",
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "6", "-hls_flags", "delete_segments",
                str(self.dir / "live.m3u8"),
            ],
            stderr=subprocess.DEVNULL,
        )  # fmt: skip
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:  # wait until a couple of segments exist, as a real live feed would have
            if len(list(self.dir.glob("*.ts"))) >= 3:
                return
            time.sleep(0.2)
        raise AssertionError("the test live source did not start")

    def stop(self) -> None:
        if self.ffmpeg and self.ffmpeg.poll() is None:
            self.ffmpeg.terminate()
            self.ffmpeg.wait(10)
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def live(tmp_path) -> Iterator[callable]:  # type: ignore[valid-type]
    sources: list[LiveSource] = []

    def make(seconds: int) -> LiveSource:
        directory = tmp_path / f"live{len(sources)}"
        directory.mkdir()
        source = LiveSource(directory, seconds)
        sources.append(source)
        source.start()
        return source

    yield make
    for source in sources:
        source.stop()


def duration(path: Path) -> float:
    return ff.probe(path).duration_s


@needs_tools
def test_a_live_stream_is_cut_into_segments_reported_as_each_one_closes(tmp_path, live):
    store = LocalStore(tmp_path / "media")
    source = live(18)
    seen: list[tuple[rec.Segment, float]] = []

    def on_segment(segment: rec.Segment) -> None:
        seen.append((segment, duration(store.path(segment.key))))  # the file must be complete when it is reported

    reason = rec.record(source.url, store, "stream1", segment_s=4, on_segment=on_segment)
    assert reason == "ended"
    assert len(seen) >= 2
    assert [s.index for s, _ in seen] == list(range(len(seen)))
    assert all(s.key == f"segments/stream1/{s.index:05d}.ts" for s, _ in seen)
    assert all(store.exists(s.key) for s, _ in seen)
    for segment, length in seen[:-1]:
        assert 3.0 <= length <= 6.5, (segment, length)  # cut at the first keyframe after 4 s
    for (a, _), (b, _) in zip(seen, seen[1:], strict=False):
        assert b.start_s == pytest.approx(a.end_s, abs=0.3)  # no gap between segments


@needs_tools
def test_the_reported_times_match_the_files(tmp_path, live):
    store = LocalStore(tmp_path / "media")
    seen: list[rec.Segment] = []
    rec.record(live(14).url, store, "s", segment_s=4, on_segment=seen.append)
    for segment in seen:
        assert duration(store.path(segment.key)) == pytest.approx(segment.end_s - segment.start_s, abs=0.6)


@needs_tools
def test_stopping_ends_the_recording_promptly_and_keeps_the_partial_last_segment(tmp_path, live):
    store = LocalStore(tmp_path / "media")
    source = live(40)
    stop = threading.Event()
    seen: list[rec.Segment] = []
    threading.Timer(9, stop.set).start()
    started = time.monotonic()
    reason = rec.record(source.url, store, "s", segment_s=4, on_segment=seen.append, stop=stop)
    assert reason == "stopped"
    assert time.monotonic() - started < 25
    assert len(seen) >= 2
    assert all(store.exists(s.key) for s in seen)
    assert duration(store.path(seen[-1].key)) > 0.5  # the last, shorter segment was kept and is readable


@needs_tools
def test_a_stream_that_cannot_be_opened_is_a_clear_error_not_a_hang(tmp_path):
    with pytest.raises(rec.RecorderError) as caught:
        rec.record(
            "hls://http://127.0.0.1:9/nothing.m3u8", LocalStore(tmp_path), "s", segment_s=4, on_segment=lambda s: None
        )
    assert "streamlink" in str(caught.value).lower()


@needs_tools
def test_an_error_in_the_callback_stops_the_recording_and_is_reported(tmp_path, live):
    def broken(segment: rec.Segment) -> None:
        raise RuntimeError("database is down")

    with pytest.raises(rec.RecorderError, match="database is down"):
        rec.record(live(30).url, LocalStore(tmp_path / "media"), "s", segment_s=4, on_segment=broken)


@needs_tools
def test_a_restarted_recording_continues_the_numbering_and_the_timeline(tmp_path, live):
    store = LocalStore(tmp_path / "media")
    seen: list[rec.Segment] = []
    rec.record(live(8).url, store, "s", segment_s=4, on_segment=seen.append, start_index=7, start_s=100.0)
    assert seen and seen[0].index == 7 and seen[0].key == "segments/s/00007.ts"
    assert seen[0].start_s == pytest.approx(100.0, abs=0.3)
    assert [x.index for x in seen] == list(range(7, 7 + len(seen)))
    assert not store.exists("segments/s/00000.ts")
