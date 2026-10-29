"""Bad words in a clip: muted in the audio, stars in the captions, nothing else touched."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import config_schema as cs
from profile_factory import build
from services.render import captions as cap
from services.render import ffmpeg as ff
from services.render import profanity as pf

RULES = cs.Profanity(terms=["anjing", "jancok", "fuck"], pad_before_s=0.1, pad_after_s=0.15)


def words(*spans):
    return [cap.Word(t, a, b) for t, a, b in spans]


# ---------- which words ----------
@pytest.mark.parametrize("text", ["anjing", "Anjing!", "ANJING,", "anjiiing", "jancoook", "Fuck", "f-u-c-k"])
def test_a_listed_word_is_found_whatever_its_case_punctuation_or_stretched_letters(text):
    assert pf.is_bad(text, RULES)


@pytest.mark.parametrize("text", ["anjay", "lord", "jancokers", "", "...", "sunfuck"])
def test_a_word_that_only_contains_one_or_is_something_else_is_left_alone(text):
    assert not pf.is_bad(text, RULES)


def test_without_terms_nothing_is_bad():
    assert not pf.is_bad("anjing", cs.Profanity())


# ---------- the mutes ----------
def test_the_mute_covers_the_word_a_little_before_and_after_in_clip_time():
    w = words(("halo", 10.0, 10.4), ("anjing", 12.0, 12.5), ("lord", 13.0, 13.4))
    assert pf.mute_ranges(w, RULES, clip_start_s=10.0, clip_s=5.0) == [(1.9, 2.65)]


def test_mutes_close_together_are_one_and_far_apart_ones_are_two():
    w = words(("anjing", 1.0, 1.4), ("jancok", 1.5, 1.9), ("fuck", 4.0, 4.3))
    assert pf.mute_ranges(w, RULES, clip_start_s=0.0, clip_s=6.0) == [(0.9, 2.05), (3.9, 4.45)]


def test_a_mute_stays_inside_the_clip():
    w = words(("anjing", 0.0, 0.2), ("fuck", 4.9, 5.4))
    spans = pf.mute_ranges(w, RULES, clip_start_s=0.0, clip_s=5.0)
    assert [x for span in spans for x in span] == pytest.approx([0.0, 0.35, 4.8, 5.0])


def test_a_word_outside_the_clip_mutes_nothing():
    assert pf.mute_ranges(words(("anjing", 20.0, 20.4)), RULES, clip_start_s=0.0, clip_s=5.0) == []


def test_mute_off_or_no_terms_means_no_mutes():
    w = words(("anjing", 1.0, 1.4))
    assert pf.mute_ranges(w, cs.Profanity(terms=["anjing"], mute=False), clip_start_s=0, clip_s=5) == []
    assert pf.mute_ranges(w, cs.Profanity(), clip_start_s=0, clip_s=5) == []


# ---------- the captions ----------
def test_the_captions_show_a_bad_word_as_its_first_letter_and_stars_keeping_the_marks_around_it():
    out = pf.mask(words(("Anjing,", 1, 2), ("lord", 2, 3), ("FUCK!", 3, 4)), RULES)
    assert [w.text for w in out] == ["A*****,", "lord", "F***!"]
    assert [(w.start_s, w.end_s) for w in out] == [(1, 2), (2, 3), (3, 4)]


def test_masking_can_be_switched_off():
    assert pf.mask(words(("anjing", 1, 2)), cs.Profanity(terms=["anjing"], mask_captions=False))[0].text == "anjing"


# ---------- the config ----------
def test_a_profile_has_no_bad_words_unless_it_lists_some(tmp_path):
    assert build(tmp_path).profanity.terms == []
    assert build(tmp_path, profanity={"terms": ["x"]}).profanity.terms == ["x"]


def test_the_mlbb_profile_lists_bad_words_so_its_clips_are_cleaned():
    (profile, *_) = cs.load_profile(cs.ROOT / "profiles" / "mlbb.yaml")
    assert "jancok" in {pf._key(t) for t in profile.profanity.terms} and profile.profanity.mute


# ---------- a real render ----------
FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe") and FONT.exists()),
    reason="needs ffmpeg and DejaVu Sans Bold",
)


def level(clip: Path, start_s: float, length_s: float) -> float:
    """The mean volume (dB) of a stretch of the clip's audio."""
    run = subprocess.run(  # noqa: S603  # nosec B603
        [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-ss", str(start_s), "-t", str(length_s), "-i", str(clip),
         "-vn", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, check=True, timeout=120,
    )  # fmt: skip
    match = re.search(r"mean_volume: (-?[\d.]+|-inf) dB", run.stderr)
    assert match, run.stderr[-300:]
    return -90.0 if match.group(1) == "-inf" else float(match.group(1))


@needs_ffmpeg
def test_the_audio_goes_silent_while_a_bad_word_is_spoken_and_stays_as_it_was_elsewhere(tmp_path):
    source = tmp_path / "tone.mp4"
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    subprocess.run(  # noqa: S603  # nosec B603
        [
            ffmpeg, "-v", "error", "-nostdin",
            "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30:d=6",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
        ],
        check=True, timeout=120,
    )  # fmt: skip
    out = tmp_path / "clip.mp4"
    profile = build(
        tmp_path, layout={"type": "center"}, captions={"font": str(FONT)}, profanity={"terms": ["anjing"]},
        output={"loudnorm": False}, branding={"logo": {"enabled": False}, "credit": {"text": None}},
    )  # fmt: skip
    ff.render_clip(profile, source, 0.0, 5.0, out, words=words(("halo", 0.2, 0.8), ("anjing", 2.0, 2.5)))
    assert level(out, 2.15, 0.3) < -60  # inside the mute (1.9 to 2.65 s)
    assert level(out, 0.3, 1.0) > -30 and level(out, 3.2, 1.0) > -30  # a sine at full scale is about -9 dB
