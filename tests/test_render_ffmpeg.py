"""The FFmpeg layer: the command it builds, and real renders of a synthetic video (FR-6)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from profile_factory import build
from services.render import ffmpeg as ff

CAM = {"x": 1440, "y": 760, "w": 480, "h": 320}
MAIN = {"x": 420, "y": 0, "w": 1080, "h": 1080}
PLAIN = {
    "captions": {"enabled": False},
    "branding": {"logo": {"enabled": False}, "credit": {"text": None}, "hook_overlay": {"enabled": False}},
}
LAYOUTS = {
    "center": {"type": "center"},
    "blur_fill": {"type": "blur_fill"},
    "split": {"type": "split", "cam": CAM, "main": MAIN},
}
needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="no ffmpeg")


# ---------- the command (pure) ---------------------------------------------------------------------
def cmd(**overrides):
    args: dict[str, Any] = {
        "ffmpeg": "/usr/bin/ffmpeg",
        "source": Path("/media/in.mp4"),
        "out": Path("/media/out.mp4.part"),
        "start_s": 12.5,
        "duration_s": 30.0,
        "graph": "[0:v]null[v]",
        "output": {"fps": 30, "crf": 20, "audio_bitrate": "160k"},
        "codec": "libx264",
        "has_audio": True,
        "loudnorm": True,
    }
    return ff.build_command(**(args | overrides))


def test_the_command_is_an_argument_list_never_a_shell_string():
    assert isinstance(cmd(), list)
    assert all(isinstance(part, str) for part in cmd())


def test_input_protocols_are_limited_to_local_files():
    c = cmd()
    assert c[c.index("-protocol_whitelist") + 1] == "file"
    assert c.index("-protocol_whitelist") < c.index("-i")  # an input option, so it must come before -i


def test_the_clip_window_is_cut_by_seeking_to_the_start_and_limiting_the_duration():
    c = cmd()
    assert c[c.index("-ss") + 1] == "0.500"  # seeks a margin before 12.5 s: the exact start is cut in the graph
    assert c[c.index("-t") + 1] == "30.000"
    assert c.index("-ss") < c.index("-i")
    graph = c[c.index("-filter_complex") + 1]
    assert graph.startswith("[0:v]trim=start=12,setpts=PTS-STARTPTS[t0];[t0]null[v]")
    assert "atrim=start=12,asetpts=PTS-STARTPTS" in c[c.index("-af") + 1]


def test_a_window_at_the_very_start_needs_no_trim():
    c = cmd(start_s=0.0)
    assert c[c.index("-ss") + 1] == "0.000" and c[c.index("-filter_complex") + 1] == "[0:v]null[v]"
    assert c[c.index("-af") + 1] == ff.LOUDNORM


def test_loudness_is_normalised_only_when_asked_and_only_with_audio():
    assert ff.LOUDNORM in cmd()[cmd().index("-af") + 1]
    assert "-af" not in cmd(
        loudnorm=False, start_s=0.0
    )  # (a later start adds the exact-start audio trim, not loudness)
    assert ff.LOUDNORM not in cmd(loudnorm=False)[cmd(loudnorm=False).index("-af") + 1]
    assert "-af" not in cmd(has_audio=False)


def test_a_clip_with_no_audio_maps_no_audio():
    assert "0:a:0?" in cmd(has_audio=True)
    assert "-c:a" not in cmd(has_audio=False)


def test_the_x264_preset_comes_from_the_profile_and_defaults_to_veryfast():
    assert cmd()[cmd().index("-preset") + 1] == "veryfast"
    slow = cmd(output={"fps": 60, "crf": 18, "preset": "slow"})
    assert slow[slow.index("-preset") + 1] == "slow" and slow[slow.index("-r") + 1] == "60"


def test_libx264_uses_crf_and_nvenc_uses_a_quality_target():
    assert "-crf" in cmd(codec="libx264")
    nvenc = cmd(codec="h264_nvenc")
    assert "-cq" in nvenc
    assert "-crf" not in nvenc


def test_output_is_yuv420p_mp4_with_faststart_so_all_four_platforms_accept_it():
    c = cmd()
    assert c[c.index("-pix_fmt") + 1] == "yuv420p"
    assert c[c.index("-f") + 1] == "mp4"
    assert "+faststart" in c


def test_ffmpeg_never_reads_from_the_terminal_and_overwrites_only_its_temporary_file():
    c = cmd()
    assert "-nostdin" in c
    assert c[-1].endswith(".part")


# ---------- picking the encoder ---------------------------------------------------------------------
def test_libx264_is_used_as_asked():
    assert ff.pick_video_codec("libx264") == "libx264"


@needs_ffmpeg
def test_nvenc_falls_back_to_libx264_when_the_gpu_is_not_usable():
    assert ff.pick_video_codec("h264_nvenc") in {"h264_nvenc", "libx264"}


# ---------- real renders ----------------------------------------------------------------------------
@pytest.fixture(scope="session")
def source(tmp_path_factory) -> Path:
    """Ten seconds of 1280x720 test pattern with a quiet 440 Hz tone."""
    path = tmp_path_factory.mktemp("media") / "source.mp4"
    subprocess.run(  # noqa: S603
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=30:duration=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=10",
            "-filter:a",
            "volume=0.02",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],  # fmt: skip
        check=True,
        timeout=120,
    )
    return path


def lufs(path: Path) -> float:
    run = subprocess.run(  # noqa: S603
        [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return float(re.findall(r"I:\s+(-?\d+(?:\.\d+)?) LUFS", run.stderr)[-1])


@needs_ffmpeg
@pytest.mark.parametrize("name", sorted(LAYOUTS))
def test_each_layout_renders_a_1080x1920_h264_aac_clip_of_the_right_length(tmp_path, source, name):
    profile = build(tmp_path, layout=LAYOUTS[name], **PLAIN)
    out = ff.render_clip(profile, source, 2.0, 5.0, tmp_path / f"{name}.mp4")
    info = ff.probe(out)
    assert (info.width, info.height) == (1080, 1920)
    assert info.video_codec == "h264"
    assert info.pix_fmt == "yuv420p"
    assert info.audio_codec == "aac"
    assert abs(info.fps - 30) < 0.01
    assert abs(info.duration_s - 3.0) < 0.2


@needs_ffmpeg
def test_loudness_is_brought_up_to_a_normal_level(tmp_path, source):
    profile = build(tmp_path, layout=LAYOUTS["center"], **PLAIN)
    out = ff.render_clip(profile, source, 0.0, 8.0, tmp_path / "loud.mp4")
    assert lufs(out) > lufs(source) + 8  # the source is about -40 LUFS; a clip should land near -14


@needs_ffmpeg
def test_loudness_is_left_alone_when_normalisation_is_off(tmp_path, source):
    profile = build(tmp_path, layout=LAYOUTS["center"], output={"loudnorm": False}, **PLAIN)
    out = ff.render_clip(profile, source, 0.0, 8.0, tmp_path / "raw.mp4")
    assert abs(lufs(out) - lufs(source)) < 3


@needs_ffmpeg
def test_a_finished_render_leaves_only_the_clip_behind(tmp_path, source):
    profile = build(tmp_path, layout=LAYOUTS["center"], **PLAIN)
    ff.render_clip(profile, source, 0.0, 2.0, tmp_path / "clip.mp4")
    assert sorted(p.name for p in tmp_path.glob("clip*")) == ["clip.mp4"]


@needs_ffmpeg
def test_a_failed_render_raises_and_leaves_nothing_behind(tmp_path):
    profile = build(tmp_path, layout=LAYOUTS["center"], **PLAIN)
    with pytest.raises(ff.RenderError):
        ff.render_clip(profile, tmp_path / "missing.mp4", 0.0, 2.0, tmp_path / "clip.mp4")
    assert list(tmp_path.glob("clip*")) == []


@needs_ffmpeg
def test_a_file_that_is_not_media_fails_cleanly_with_a_readable_message(tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video at all")
    profile = build(tmp_path, layout=LAYOUTS["center"], **PLAIN)
    with pytest.raises(ff.RenderError, match="junk.mp4|Invalid data|probe"):
        ff.render_clip(profile, junk, 0.0, 2.0, tmp_path / "clip.mp4")


@needs_ffmpeg
@pytest.mark.parametrize(("start", "end"), [(5.0, 5.0), (6.0, 2.0), (-1.0, 3.0), (8.0, 40.0), (0.0, 181.0)])
def test_an_impossible_window_is_rejected_before_ffmpeg_runs(tmp_path, source, start, end):
    profile = build(tmp_path, layout=LAYOUTS["center"], **PLAIN)
    with pytest.raises(ValueError, match="window"):
        ff.render_clip(profile, source, start, end, tmp_path / "clip.mp4")
    assert list(tmp_path.glob("clip*")) == []


@needs_ffmpeg
def test_a_local_playlist_cannot_make_ffmpeg_request_a_url(tmp_path):
    hits: list[str] = []

    class Counter(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - the name http.server looks for
            hits.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Counter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        playlist = tmp_path / "evil.m3u8"
        url = f"http://127.0.0.1:{server.server_port}/segment.ts"
        playlist.write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\n{url}\n#EXT-X-ENDLIST\n")
        profile = build(tmp_path, layout=LAYOUTS["center"], **PLAIN)
        with pytest.raises(ff.RenderError):
            ff.render_clip(profile, playlist, 0.0, 2.0, tmp_path / "clip.mp4")
    finally:
        server.shutdown()
        server.server_close()
    assert hits == []  # FFmpeg never even asked


def test_a_recap_may_be_longer_than_an_ordinary_clip_when_its_own_ceiling_says_so():
    ff.check_window(0.0, 250.0, 300.0, max_s=300)  # accepted
    with pytest.raises(ValueError, match="longer"):
        ff.check_window(0.0, 250.0, 300.0)  # an ordinary clip stays under its own ceiling


# ---------- a video whose size the first quick look cannot read ----------
class _Run:
    def __init__(self, width: int, height: int = 1080):
        self.returncode, self.stderr = 0, ""
        video = {"codec_type": "video", "codec_name": "h264", "width": width, "height": height}
        video |= {"avg_frame_rate": "60/1", "pix_fmt": "yuv420p"}
        self.stdout = json.dumps(
            {"streams": [video, {"codec_type": "audio", "codec_name": "aac"}], "format": {"duration": "600.0"}}
        )


def test_a_size_of_zero_is_probed_again_with_a_longer_look_before_giving_up(monkeypatch):
    """Some recorded segments (a Streamlink TS that does not start with its stream headers) read as 0x0 with ffprobe's
    default look and as 1920x1080 with a longer one."""
    commands: list[list[str]] = []

    def fake_run(command, **kw):
        commands.append(command)
        return _Run(0, 0) if len(commands) == 1 else _Run(1920)

    monkeypatch.setattr(ff.subprocess, "run", fake_run)
    monkeypatch.setattr(ff.shutil, "which", lambda name: f"/bin/{name}")
    info = ff.probe(Path("/media/segments/x/00014.ts"))
    assert (info.width, info.height) == (1920, 1080) and len(commands) == 2
    assert "-analyzeduration" not in commands[0] and "-probesize" not in commands[0]
    assert "-analyzeduration" in commands[1] and "-probesize" in commands[1]


def test_a_good_first_look_is_the_only_look(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(ff.subprocess, "run", lambda command, **kw: calls.append(1) or _Run(1920))
    monkeypatch.setattr(ff.shutil, "which", lambda name: f"/bin/{name}")
    assert ff.probe(Path("/m/a.ts")).width == 1920 and calls == [1]


def test_a_size_that_stays_zero_is_a_clear_error_not_a_zero_by_zero_frame(monkeypatch):
    monkeypatch.setattr(ff.subprocess, "run", lambda command, **kw: _Run(0, 0))
    monkeypatch.setattr(ff.shutil, "which", lambda name: f"/bin/{name}")
    with pytest.raises(ff.RenderError, match="size"):
        ff.probe(Path("/m/a.ts"))


# ---------- audio instagram accepts: AAC at 48 kHz at most ----------
def audio_rate(path: Path) -> int:
    run = subprocess.run(  # noqa: S603
        [shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=sample_rate", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60, check=True,
    )  # fmt: skip
    return int(run.stdout.strip())


def test_the_command_asks_for_48_khz_audio_unless_the_profile_says_otherwise():
    c = cmd()
    assert c[c.index("-ar") + 1] == "48000"
    c = cmd(output={"fps": 30, "crf": 20, "audio_sample_rate": 44100})
    assert c[c.index("-ar") + 1] == "44100"
    assert "-ar" not in cmd(has_audio=False)


@needs_ffmpeg
@pytest.mark.parametrize("loudnorm", [True, False])
def test_the_rendered_clip_has_48_khz_audio_with_or_without_loudness_normalisation(tmp_path, source, loudnorm):
    """loudnorm works at 192 kHz inside, and left alone the encoder keeps that rate: a 96 kHz clip made Instagram fail
    with ProcessingFailedError (its Reels limit is 48 kHz)."""
    profile = build(tmp_path, layout=LAYOUTS["center"], output={"loudnorm": loudnorm}, **PLAIN)
    out = ff.render_clip(profile, source, 0.0, 5.0, tmp_path / "rate.mp4")
    assert audio_rate(out) == 48000
