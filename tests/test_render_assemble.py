"""Joining pieces of several recording files into one source for a recap, before the usual render runs on it."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from common.media import LocalStore
from services.render import assemble
from services.render import ffmpeg as ff

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")


def make(path: Path, *, size: str, rate: int, tone: int, seconds: int = 6) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603  # nosec B603
        [
            shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}",
            "-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=44100", "-t", str(seconds), "-c:v", "libx264",
            "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True, timeout=120,
    )  # fmt: skip
    return path


@pytest.fixture
def store(tmp_path):
    media = LocalStore(tmp_path / "media")
    make(media.path("segments/s/00000.ts"), size="1280x720", rate=30, tone=440)
    make(media.path("segments/s/00001.ts"), size="1280x720", rate=30, tone=880)
    make(media.path("segments/s/00002.ts"), size="640x360", rate=25, tone=660)  # a restart came back smaller and slower
    return media


def piece(index, start, end):
    return assemble.PieceIn(source_key=f"segments/s/{index:05d}.ts", start_s=start, end_s=end)


def test_pieces_from_several_files_become_one_video_as_long_as_they_add_up_to(store, tmp_path):
    out = assemble.join(store, [piece(0, 1, 3), piece(1, 0, 2), piece(0, 4, 5)], tmp_path / "joined.mp4")
    info = ff.probe(out)
    assert info.duration_s == pytest.approx(5.0, abs=0.4) and info.has_audio
    assert (info.width, info.height) == (1280, 720) and info.fps == pytest.approx(30, abs=0.5)


def test_a_piece_that_is_smaller_or_slower_is_brought_to_the_size_and_rate_of_the_first(store, tmp_path):
    out = assemble.join(store, [piece(0, 0, 2), piece(2, 1, 3)], tmp_path / "joined.mp4")
    info = ff.probe(out)
    assert (info.width, info.height) == (1280, 720) and info.fps == pytest.approx(30, abs=0.5)
    assert info.duration_s == pytest.approx(4.0, abs=0.4)


def test_the_audio_of_each_piece_is_kept_in_order(store, tmp_path):
    """The first piece is a 440 Hz tone and the second an 880 Hz one: the result must play them in that order."""
    out = assemble.join(store, [piece(0, 0, 2), piece(1, 0, 2)], tmp_path / "joined.mp4")
    first, second = tmp_path / "first.wav", tmp_path / "second.wav"
    for target, start in ((first, 0.2), (second, 2.2)):
        subprocess.run(  # noqa: S603  # nosec B603
            [
                shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", "1.5",
                "-i", str(out), "-vn", str(target),
            ],
            check=True,
        )  # fmt: skip

    def tone_hz(path: Path) -> float:
        """The pitch of a pure tone, from how often the wave crosses zero going up."""
        import array
        import wave

        with wave.open(str(path)) as w:
            frames, rate, channels = w.getnframes(), w.getframerate(), w.getnchannels()
            samples = array.array("h", w.readframes(frames))[0::channels]
        return sum(1 for a, b in zip(samples, samples[1:], strict=False) if a < 0 <= b) / (frames / rate)

    assert tone_hz(first) == pytest.approx(440, rel=0.1) and tone_hz(second) == pytest.approx(880, rel=0.1)


def test_a_piece_whose_file_is_gone_is_a_clear_error(store, tmp_path):
    with pytest.raises(assemble.AssembleError, match="not in the media store"):
        assemble.join(store, [piece(0, 0, 2), piece(9, 0, 2)], tmp_path / "joined.mp4")


def test_a_piece_outside_its_file_is_an_error_and_leaves_no_result_behind(store, tmp_path):
    bad = assemble.PieceIn(source_key="segments/s/00000.ts", start_s=50.0, end_s=52.0)  # past the end of a 6 s file
    out = tmp_path / "joined.mp4"
    with pytest.raises(assemble.AssembleError, match="outside"):
        assemble.join(store, [piece(0, 0, 2), bad], out)
    assert not out.exists() and not out.with_name("joined.mp4.part").exists()


def stream_seconds(path: Path, kind: str) -> float:
    run = subprocess.run(  # noqa: S603  # nosec B603
        [shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", f"{kind}:0", "-show_entries",
         "stream=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60, check=True,
    )  # fmt: skip
    return float(run.stdout.strip())


def test_many_short_pieces_add_up_without_drift_between_picture_and_sound(store, tmp_path):
    pieces = [piece(0, 0.5, 2.0), piece(1, 1.0, 2.5), piece(0, 3.0, 4.5), piece(1, 3.5, 5.0), piece(0, 1.0, 2.5)]
    out = assemble.join(store, pieces, tmp_path / "joined.mp4")
    assert ff.probe(out).duration_s == pytest.approx(7.5, abs=0.3)
    assert abs(stream_seconds(out, "v") - stream_seconds(out, "a")) < 0.15


def test_each_piece_is_cut_by_its_own_process_with_one_input_so_memory_never_holds_two_pieces(tmp_path):
    """The old single process opened every piece at once, and its concat filter queued the frames of the later ones
    while it played the first: at 1080p60 six pieces filled a 1.5 GB container and the recap died of memory."""
    cmd = assemble.piece_command(
        "/usr/bin/ffmpeg", Path("/media/a.ts"), piece(0, 12.5, 30.0), 1920, 1080, 60.0, tmp_path / "p.ts"
    )
    assert cmd.count("-i") == 1 and isinstance(cmd, list)
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "file"
    assert "concat" not in cmd[cmd.index("-filter_complex") + 1]
    assert cmd[cmd.index("-r") + 1] == "60.000"  # the rate is stated, never left to the encoder to guess
    assert cmd[cmd.index("-t", cmd.index("-map")) + 1] == "17.500"  # the piece's exact length, picture and sound


def test_the_finished_pieces_are_joined_without_decoding_anything(tmp_path):
    cmd = assemble.concat_command("/usr/bin/ffmpeg", tmp_path / "list.txt", tmp_path / "x.part")
    assert cmd[cmd.index("-f") + 1] == "concat" and cmd[cmd.index("-c") + 1] == "copy"
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "file" and "-filter_complex" not in cmd


def test_the_pieces_made_on_the_way_are_cleaned_up_when_a_later_one_fails(store, tmp_path, monkeypatch):
    real, calls = assemble._run, []

    def flaky(cmd, what):
        calls.append(what)
        if len(calls) == 2:  # the second piece's process dies
            raise assemble.AssembleError(f"ffmpeg could not make {what}: boom")
        real(cmd, what)

    monkeypatch.setattr(assemble, "_run", flaky)
    out = tmp_path / "out" / "joined.mp4"
    with pytest.raises(assemble.AssembleError, match="boom"):
        assemble.join(store, [piece(0, 0, 2), piece(1, 0, 2), piece(0, 3, 5)], out)
    assert calls == ["piece 1", "piece 2"]  # it stopped at the first failure
    assert sorted(p.name for p in out.parent.iterdir()) == []  # no half result is left next to the output
    assert sorted(p.name for p in Path(store.path("render-tmp")).iterdir()) == []  # nor any finished piece or list


def test_the_pieces_are_made_on_the_media_disk_and_only_the_result_lands_next_to_the_output(
    store, tmp_path, monkeypatch
):
    made: list[Path] = []
    real = assemble._run

    def spy(cmd, what):
        made.append(Path(cmd[-1]))
        real(cmd, what)

    monkeypatch.setattr(assemble, "_run", spy)
    out = tmp_path / "out" / "joined.mp4"
    assemble.join(store, [piece(0, 0, 2), piece(1, 0, 2)], out)
    scratch = Path(store.path("render-tmp"))
    assert all(scratch in p.parents for p in made[:2])  # the two pieces: on the media disk
    assert made[2].parent == out.parent  # the join: beside the output
    assert sorted(p.name for p in out.parent.iterdir()) == ["joined.mp4"]


def test_a_work_folder_left_by_a_killed_join_is_swept_away_later(store, tmp_path):
    import os
    import time

    scratch = Path(store.path("render-tmp"))
    old, fresh = scratch / "assemble-old", scratch / "assemble-fresh"
    for d in (old, fresh):
        d.mkdir(parents=True)
        (d / "000.ts").write_bytes(b"x")
    long_ago = time.time() - 7 * 3600
    os.utime(old, (long_ago, long_ago))
    assemble.join(store, [piece(0, 0, 1)], tmp_path / "joined.mp4")
    assert not old.exists() and fresh.exists()  # only the stale one goes
