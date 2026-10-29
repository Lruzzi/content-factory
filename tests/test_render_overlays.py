"""Real renders with captions, hook, credit and logo. The checks look at pixels: the text must show up when
it is spoken, and nothing may land outside the safe zone (FR-6)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import config_schema as cs
from profile_factory import build
from services.render import captions as cap
from services.render import ffmpeg as ff

FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe") and FONT.exists()),
    reason="needs ffmpeg and DejaVu Sans Bold",
)

W, H = 1080, 1920
SAFE = {"top": 0.12, "bottom": 0.20, "right": 0.14, "left": 0.04}
X0, Y0, X1, Y1 = W * SAFE["left"], H * SAFE["top"], W * (1 - SAFE["right"]), H * (1 - SAFE["bottom"])
TOL = 3  # pixels of slack for anti-aliasing and rounding
BOX_CAM = {"x": 1440, "y": 760, "w": 480, "h": 320}
BOX_MAIN = {"x": 420, "y": 0, "w": 1080, "h": 1080}
SPEECH = [cap.Word("savage", 0.2, 0.9), cap.Word("lord", 1.0, 1.6), cap.Word("comeback", 1.8, 2.6)]


def ffmpeg_run(*args, stdout=None):
    return subprocess.run(  # noqa: S603  # nosec B603
        [shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-nostdin", *args], check=True, timeout=120, stdout=stdout
    )


@pytest.fixture(scope="session")
def black(tmp_path_factory) -> Path:
    """Six seconds of black, so every bright pixel in a render belongs to an overlay."""
    path = tmp_path_factory.mktemp("overlay-media") / "black.mp4"
    ffmpeg_run(
        "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30:d=6",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    )  # fmt: skip
    return path


@pytest.fixture(scope="session")
def red_logo(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("overlay-logo") / "logo.png"
    ffmpeg_run("-f", "lavfi", "-i", "color=c=red:s=200x100", "-frames:v", "1", str(path))
    return path


def look(out: Path, *, logo: Path | None = None, **kw):
    """Profile with the DejaVu font, an optional logo, and any branding/caption overrides."""
    branding = {
        "logo": {"enabled": logo is not None, "path": str(logo) if logo else None, "width": 140, "opacity": 1.0}
    }
    branding = cs.deep_merge(branding, kw.pop("branding", {}))
    return build(
        out.parent,
        layout={"type": "center"},
        captions={"font": str(FONT)} | kw.pop("captions", {}),
        branding=branding,
        **kw,
    )


def render(tmp_path, black, name="clip.mp4", *, words=SPEECH, hook=None, logo=None, **kw):
    out = tmp_path / name
    profile = look(out, logo=logo, **kw)
    return ff.render_clip(profile, black, 0.0, 4.0, out, words=words, hook=hook)


def frame(clip: Path, t: float, pix_fmt: str) -> bytes:
    run = subprocess.run(  # noqa: S603  # nosec B603
        [
            shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-nostdin", "-ss", str(t), "-i", str(clip),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", pix_fmt, "-",
        ],
        capture_output=True, check=True, timeout=120,
    )  # fmt: skip
    return run.stdout


def bright_box(gray: bytes, threshold: int = 100, rows: range | None = None):
    """(left, top, right, bottom) of the pixels brighter than the threshold, or None."""
    left, right, top, bottom = W, -1, H, -1
    for y in rows or range(H):
        row = gray[y * W : (y + 1) * W]
        if max(row) > threshold:
            xs = [x for x, v in enumerate(row) if v > threshold]
            left, right = min(left, xs[0]), max(right, xs[-1])
            top, bottom = min(top, y), max(bottom, y)
    return None if right < 0 else (left, top, right, bottom)


def inside_safe(box) -> bool:
    return box[0] >= X0 - TOL and box[1] >= Y0 - TOL and box[2] <= X1 + TOL and box[3] <= Y1 + TOL


def red_box(rgb: bytes):
    left, right, top, bottom = W, -1, H, -1
    for y in range(H):
        row = rgb[y * W * 3 : (y + 1) * W * 3]
        reds = [x for x in range(W) if row[3 * x] > 200 and row[3 * x + 1] < 60 and row[3 * x + 2] < 60]
        if reds:
            left, right = min(left, reds[0]), max(right, reds[-1])
            top, bottom = min(top, y), max(bottom, y)
    return None if right < 0 else (left, top, right, bottom)


# ---------- captions, hook and credit ------------------------------------------------------------------
def test_all_text_overlays_stay_inside_the_safe_zone(tmp_path, black):
    clip = render(
        tmp_path, black, hook="Wait for the last second", branding={"credit": {"text": "Full stream: Leo Kabuki"}}
    )
    for t in (0.5, 1.2, 2.2):
        box = bright_box(frame(clip, t, "gray"))
        assert box is not None, t
        assert inside_safe(box), (t, box)


def test_a_caption_shows_while_its_word_is_spoken_and_not_before_or_after(tmp_path, black):
    clip = render(tmp_path, black, branding={"credit": {"text": None}})
    band = range(round(H * 0.62) - 90, round(H * 0.62) + 90)
    assert bright_box(frame(clip, 0.5, "gray"), rows=band) is not None  # "savage" is on
    assert bright_box(frame(clip, 0.05, "gray"), rows=band) is None  # before the first word
    assert bright_box(frame(clip, 3.5, "gray"), rows=band) is None  # after the last word


def test_the_caption_is_centred_between_the_safe_margins(tmp_path, black):
    clip = render(tmp_path, black, branding={"credit": {"text": None}})
    left, _, right, _ = bright_box(frame(clip, 1.2, "gray"), rows=range(1100, 1300))
    centre = (left + right) / 2
    assert abs(centre - (X0 + X1) / 2) < 40


def test_the_hook_is_at_the_top_for_its_seconds_only(tmp_path, black):
    clip = render(tmp_path, black, hook="Wait for it", words=[], branding={"credit": {"text": None}})
    top = range(round(Y0) - TOL, 520)
    box = bright_box(frame(clip, 1.0, "gray"), rows=top)
    assert box is not None
    assert box[1] >= Y0 - TOL
    assert bright_box(frame(clip, 3.0, "gray"), rows=top) is None


def test_the_credit_stays_for_the_whole_clip_above_the_bottom_margin(tmp_path, black):
    clip = render(tmp_path, black, words=[], branding={"credit": {"text": "Full stream: Leo Kabuki", "size": 34}})
    for t in (0.5, 3.5):
        box = bright_box(frame(clip, t, "gray"), rows=range(1300, H))
        assert box is not None, t
        assert box[3] <= Y1 + TOL, (t, box)


def test_hostile_transcript_text_is_drawn_as_plain_small_text(tmp_path, black):
    hostile = [cap.Word("{\\fs900\\pos(5,5)}BOOM", 0.2, 1.5), cap.Word("x\\Ny", 1.6, 2.5)]
    # uppercase off: capitals would turn \fs900 into the inert \FS900 and hide the attack
    clip = render(tmp_path, black, words=hostile, captions={"uppercase": False}, branding={"credit": {"text": None}})
    for t in (0.8, 2.0):
        box = bright_box(frame(clip, t, "gray"))
        assert box is not None
        assert inside_safe(box), (t, box)


def test_words_are_timed_on_the_same_clock_as_the_clip_window(tmp_path, black):
    late = [cap.Word("later", 2.2, 2.9)]  # the clip starts at second 2 of the source, so this is 0.2 s in
    out = tmp_path / "late.mp4"
    clip = ff.render_clip(look(out, branding={"credit": {"text": None}}), black, 2.0, 5.0, out, words=late)
    band = range(round(H * 0.62) - 90, round(H * 0.62) + 90)
    assert bright_box(frame(clip, 0.5, "gray"), rows=band) is not None
    assert bright_box(frame(clip, 2.5, "gray"), rows=band) is None


def test_captions_can_be_switched_off(tmp_path, black):
    clip = render(tmp_path, black, captions={"enabled": False}, branding={"credit": {"text": None}})
    assert bright_box(frame(clip, 1.2, "gray")) is None


# ---------- the logo -------------------------------------------------------------------------------------
@pytest.mark.parametrize("position", ["top_right", "top_left", "bottom_right", "bottom_left"])
def test_the_logo_sits_in_its_corner_of_the_safe_zone(tmp_path, black, red_logo, position):
    clip = render(
        tmp_path, black, words=[], logo=red_logo, branding={"credit": {"text": None}, "logo": {"position": position}}
    )
    box = red_box(frame(clip, 1.0, "rgb24"))
    assert box is not None
    assert inside_safe(box), box
    assert abs((box[2] - box[0]) - 140) <= 3
    horizontal, vertical = position.split("_")[1], position.split("_")[0]
    assert (box[2] >= X1 - TOL) if horizontal == "right" else (box[0] <= X0 + TOL)
    assert (box[1] <= Y0 + TOL) if vertical == "top" else (box[3] >= Y1 - TOL)


def test_the_logo_opacity_is_applied(tmp_path, black, red_logo):
    clip = render(
        tmp_path, black, words=[], logo=red_logo, branding={"credit": {"text": None}, "logo": {"opacity": 0.5}}
    )
    rgb = frame(clip, 1.0, "rgb24")
    x, y = round(X1) - 70, round(Y0) + 35  # inside the logo
    assert 90 < rgb[(y * W + x) * 3] < 170


def test_a_logo_file_that_is_missing_fails_the_render_with_a_clear_message(tmp_path, black):
    out = tmp_path / "clip.mp4"
    profile = look(out, logo=tmp_path / "no-logo.png")
    with pytest.raises(ff.AssetError, match="no-logo.png"):
        ff.render_clip(profile, black, 0.0, 2.0, out)
    assert list(tmp_path.glob("clip*")) == []


def test_a_missing_caption_font_fails_the_render_with_a_clear_message(tmp_path, black):
    out = tmp_path / "clip.mp4"
    profile = look(out, captions={"font": str(tmp_path / "Montserrat-ExtraBold.ttf")})
    with pytest.raises(cap.FontError, match="Montserrat"):
        ff.render_clip(profile, black, 0.0, 2.0, out, words=SPEECH)
    assert list(tmp_path.glob("clip*")) == []


def test_no_overlay_assets_are_needed_when_every_overlay_is_off(tmp_path, black):
    out = tmp_path / "plain.mp4"
    profile = build(
        tmp_path,
        layout={"type": "center"},
        captions={"enabled": False, "font": "/nowhere.ttf"},
        branding={"logo": {"enabled": False}, "credit": {"text": None}, "hook_overlay": {"enabled": False}},
    )
    assert ff.render_clip(profile, black, 0.0, 2.0, out).exists()


@pytest.mark.parametrize(("layout", "y"), [({"type": "center"}, 0.62), ({"type": "split", "split_ratio": 0.4}, 0.435)])
def test_captions_sit_under_the_cam_panel_on_a_split_layout_and_at_y_on_any_other(tmp_path, layout, y):
    profile = build(
        tmp_path, layout=layout | ({"cam": BOX_CAM, "main": BOX_MAIN} if layout["type"] == "split" else {}),
        captions={"font": str(FONT), "y": 0.62, "below_cam": 0.035}, branding={"credit": {"text": None}},
    )  # fmt: skip
    work = tmp_path / "w"
    work.mkdir()
    assert ff._text_overlay(profile, work, [cap.Word("savage", 0.2, 0.9)], None, 0.0, 4.0)
    assert f",{round(H * y)})" in (work / "captions.ass").read_text()
