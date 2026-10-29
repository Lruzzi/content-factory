"""A clip must start moving at once. Seeking into an MPEG-TS segment with few keyframes lands late: the audio starts on
time and the video waits for the next keyframe, so the first frame was repeated for seconds while the sound played."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from profile_factory import build
from services.render import assemble, facetrack, seek
from services.render import ffmpeg as ff

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="no ffmpeg")
PLAIN = {
    "captions": {"enabled": False},
    "branding": {"logo": {"enabled": False}, "credit": {"text": None}, "hook_overlay": {"enabled": False}},
}


def sparse_ts(path: Path, seconds: int = 80, keyframe_every_s: int = 10) -> Path:
    """Moving video with a keyframe only every 10 s, like what YouTube serves and Streamlink records, as MPEG-TS."""
    gop = 25 * keyframe_every_s
    subprocess.run(  # noqa: S603  # nosec B603
        [
            shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", str(seconds), "-c:v", "libx264",
            "-preset", "ultrafast", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", "-c:a", "aac",
            "-f", "mpegts", str(path),
        ],
        check=True, timeout=120,
    )  # fmt: skip
    return path


def frozen_at_start(video: Path, look_s: float = 9.0) -> float:
    """Seconds of unmoving picture at the very start (0 when it moves from the first frame)."""
    run = subprocess.run(  # noqa: S603  # nosec B603
        [
            shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-nostdin", "-t", str(look_s), "-i", str(video),
            "-vf", "freezedetect=n=-60dB:d=0.4", "-an", "-f", "null", "-",
        ],
        capture_output=True, text=True, check=False, timeout=120,
    )  # fmt: skip
    starts = [float(line.split("freeze_start:")[1]) for line in run.stderr.splitlines() if "freeze_start" in line]
    durations = [
        float(line.split("freeze_duration:")[1].split()[0])
        for line in run.stderr.splitlines()
        if "freeze_duration" in line
    ]
    return durations[0] if starts and starts[0] < 0.2 and durations else 0.0


# ---------- the split between seeking and trimming ---------------------------------------------------------------
def test_a_start_is_split_into_a_coarse_seek_and_an_exact_trim_that_add_up_to_it():
    assert seek.split(100.0) == (100.0 - seek.MARGIN_S, seek.MARGIN_S)
    assert seek.split(5.0) == (0.0, 5.0)
    assert seek.split(0.0) == (0.0, 0.0)
    coarse, trim = seek.split(33.25)
    assert coarse + trim == pytest.approx(33.25)


def test_the_margin_is_longer_than_the_keyframe_spacing_of_what_is_recorded_or_downloaded():
    assert seek.MARGIN_S >= 10


# ---------- a rendered clip ---------------------------------------------------------------------------------------
@needs_ffmpeg
@pytest.mark.parametrize("start", [0.0, 5.0, 17.3, 33.0, 52.0])
def test_a_clip_cut_from_a_segment_with_sparse_keyframes_moves_from_its_first_frame(tmp_path, start):
    source = sparse_ts(tmp_path / "segment.ts")
    out = ff.render_clip(
        build(tmp_path, layout={"type": "center"}, **PLAIN), source, start, start + 12, tmp_path / "clip.mp4"
    )
    assert frozen_at_start(out) == 0.0


@needs_ffmpeg
def test_the_clip_still_has_the_length_and_the_audio_that_were_asked_for(tmp_path):
    source = sparse_ts(tmp_path / "segment.ts")
    out = ff.render_clip(build(tmp_path, layout={"type": "center"}, **PLAIN), source, 33.0, 45.0, tmp_path / "clip.mp4")
    info = ff.probe(out)
    assert info.duration_s == pytest.approx(12.0, abs=0.3) and info.has_audio


# ---------- a recap: pieces joined ----------------------------------------------------------------------------------
@needs_ffmpeg
def test_every_piece_of_a_joined_recap_starts_moving_at_once(tmp_path):
    sparse_ts(tmp_path / "00000.ts")
    store = type("Store", (), {"path": lambda self, key: tmp_path / key.split("/")[-1]})()
    pieces = [
        assemble.PieceIn(source_key="segments/s/00000.ts", start_s=17.3, end_s=22.3),
        assemble.PieceIn(source_key="segments/s/00000.ts", start_s=33.0, end_s=38.0),
    ]
    joined = assemble.join(store, pieces, tmp_path / "joined.mp4")
    assert frozen_at_start(joined, 4.0) == 0.0
    run = subprocess.run(  # noqa: S603  # nosec B603
        [
            shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-nostdin", "-i", str(joined),
            "-vf", "freezedetect=n=-60dB:d=0.4", "-an", "-f", "null", "-",
        ],
        capture_output=True, text=True, check=False, timeout=120,
    )  # fmt: skip
    assert "freeze_start" not in run.stderr  # no frozen stretch anywhere, the join included


# ---------- the face finder looks at the same frames ----------------------------------------------------------------
def test_the_face_finder_seeks_with_the_same_margin_and_trims_exactly():
    command = facetrack.frames_command("/usr/bin/ffmpeg", Path("/m/in.ts"), 40.0, 52.0, fps=4.0, width=640, height=360)
    assert command[command.index("-ss") + 1] == f"{40.0 - seek.MARGIN_S:.3f}"
    assert command[command.index("-t") + 1] == f"{12.0 + seek.MARGIN_S:.3f}"
    assert f"trim=start={seek.MARGIN_S:g}" in command[command.index("-vf") + 1]
