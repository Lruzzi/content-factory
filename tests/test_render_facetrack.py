"""Following the speaker: a full-height 9:16 window that slides sideways to keep the face in view. The planning is pure
(no video, no detector); the FFmpeg expression is checked on real pixels."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import config_schema as cs
from services.render import facetrack as ft
from services.render import layout as lo

SRC_W, SRC_H = 1920, 1080
CROP_W = 608  # a 9:16 column of a 1080-high frame
FPS = 4.0


def face(cx, w=0.08, score=0.9):
    return ft.Face(cx=cx, w=w, score=score)


def plan(samples, **kw):
    options = {"fps": FPS, "src_w": SRC_W, "src_h": SRC_H, "crop_w": CROP_W, "smoothing": 0.9, "max_pan_px_per_s": 400}
    return ft.plan(samples, **(options | kw))


def positions(knots, times):
    return [ft.position_at(knots, t) for t in times]


# ---------- planning ----------------------------------------------------------
def test_a_face_that_stays_still_gives_a_window_that_stays_still_centred_on_it():
    knots = plan([[face(0.5)]] * 40)
    xs = positions(knots, [i / FPS for i in range(40)])
    assert max(xs) - min(xs) < 2 and xs[0] == pytest.approx(SRC_W / 2 - CROP_W / 2, abs=2)
    assert len(knots) <= 3  # a still shot is a still window, not forty keyframes


def test_a_window_never_leaves_the_frame_even_for_a_face_at_the_edge():
    for cx in (0.0, 0.02, 0.98, 1.0):
        xs = positions(plan([[face(cx)]] * 20), [i / FPS for i in range(20)])
        assert all(0 <= x <= SRC_W - CROP_W for x in xs), cx


def test_a_speaker_who_walks_across_is_followed_smoothly_and_never_faster_than_the_limit():
    samples = [[face(0.3 + 0.4 * i / 39)] for i in range(40)]  # 0.3 -> 0.7 of the width over ten seconds
    xs = positions(plan(samples), [i / FPS for i in range(40)])
    steps = [abs(b - a) for a, b in zip(xs, xs[1:], strict=False)]
    assert max(steps) <= 400 / FPS + 1 and xs[-1] > xs[0] + 150  # it did move, and not too fast


def test_small_wobbles_of_the_face_do_not_move_the_window():
    samples = [[face(0.5 + (0.01 if i % 2 else -0.01))] for i in range(40)]
    xs = positions(plan(samples), [i / FPS for i in range(40)])
    assert max(xs) - min(xs) < 2


def test_a_camera_cut_to_the_other_host_snaps_at_once_instead_of_panning_across():
    left, right = [[face(0.25)]] * 20, [[face(0.75)]] * 20
    knots = plan(left + right)
    before, after = ft.position_at(knots, 4.7), ft.position_at(knots, 5.3)
    assert after - before == pytest.approx((0.75 - 0.25) * SRC_W, abs=4)
    times = [4.6 + k * 0.01 for k in range(40)]
    moving = [
        t
        for t, a, b in zip(times, positions(knots, times), positions(knots, times)[1:], strict=False)
        if abs(b - a) > 20
    ]
    assert moving and max(moving) - min(moving) <= 0.05  # all of it within about one frame: a snap, not a slide


def test_when_nobody_is_found_the_window_holds_where_it_was():
    samples = [[face(0.3)]] * 10 + [[]] * 10
    xs = positions(plan(samples), [i / FPS for i in range(20)])
    assert max(xs) - min(xs) < 2


def test_with_no_face_at_all_the_window_is_centred():
    xs = positions(plan([[]] * 10), [0.0, 1.0, 2.0])
    assert all(x == pytest.approx((SRC_W - CROP_W) / 2, abs=2) for x in xs)


def test_with_several_faces_the_largest_confident_one_is_followed():
    samples = [[face(0.2, w=0.05), face(0.7, w=0.12)]] * 10
    assert plan(samples)[0][1] == pytest.approx(0.7 * SRC_W - CROP_W / 2, abs=2)


def test_a_weak_detection_is_ignored_for_a_clearer_one():
    samples = [[face(0.2, w=0.2, score=0.3), face(0.7, w=0.08, score=0.9)]] * 10
    assert plan(samples)[0][1] == pytest.approx(0.7 * SRC_W - CROP_W / 2, abs=2)


def test_knots_are_in_time_order_and_start_at_zero():
    knots = plan([[face(0.3 + 0.01 * i)] for i in range(30)])
    times = [t for t, _ in knots]
    assert times[0] == 0 and times == sorted(times)


# ---------- the FFmpeg expression ----------------------------------------------
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def ramp_video(path: Path) -> Path:
    """A 640x360 video whose brightness grows from left to right: the first column of a crop tells where it started."""
    subprocess.run(  # noqa: S603  # nosec B603
        [
            shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=25:d=6",
            "-vf", "geq=lum='255*X/W':cb=128:cr=128", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "5",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, timeout=60,
    )  # fmt: skip
    return path


def brightness_at(video: Path, expression: str, *, seek: float = 0.0, second: float = 0.0) -> int:
    """Brightness of the first column of the crop, in the first frame at or after `second` of the filter clock."""
    done = subprocess.run(  # noqa: S603  # nosec B603
        [
            shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-ss", f"{seek}", "-i", str(video), "-vf",
            f"crop=w=202:h=360:x='{expression}':y=0,select='gte(t\\,{second})',crop=1:1:0:100,format=gray",
            "-frames:v", "1", "-f", "rawvideo", "-",
        ],
        capture_output=True, check=True, timeout=60,
    )  # fmt: skip
    return done.stdout[0]


@needs_ffmpeg
def test_the_expression_runs_on_the_clip_clock_even_when_the_input_is_seeked(tmp_path):
    video = ramp_video(tmp_path / "ramp.mp4")
    expression = ft.expression([(0.0, 0.0), (2.0, 400.0)])
    # seeking to 3 s restarts the filter clock: one second in, the window is half way (x = 200), not at the end (400)
    assert brightness_at(video, expression, seek=3.0, second=1.0) == pytest.approx(brightness_at(video, "200"), abs=4)
    assert brightness_at(video, expression, seek=3.0, second=1.0) != pytest.approx(brightness_at(video, "400"), abs=4)


@needs_ffmpeg
def test_the_expression_is_a_straight_line_between_two_keyframes(tmp_path):
    video = ramp_video(tmp_path / "ramp.mp4")
    expression = ft.expression([(0.0, 100.0), (2.0, 300.0)])
    for second, x in ((0.0, 100), (1.0, 200), (2.0, 300)):
        assert brightness_at(video, expression, second=second) == pytest.approx(brightness_at(video, str(x)), abs=4)


def test_the_expression_with_one_knot_is_a_constant():
    assert ft.expression([(0.0, 123.0)]) == "123"


def test_a_long_path_becomes_one_nested_expression_with_every_knot_in_it():
    knots = [(float(i), float(i * 10)) for i in range(30)]
    expression = ft.expression(knots)
    assert expression.count("if(") == 29 and "t" in expression


# ---------- the layout and the render -------------------------------------------
def test_the_facetrack_graph_crops_a_moving_column_then_scales_to_the_output():
    layout = cs.Layout(type="facetrack")
    knots = [(0.0, 400.0), (3.0, 700.0)]
    g = lo.filter_graph(layout, src_w=SRC_W, src_h=SRC_H, out_w=1080, out_h=1920, track=knots)
    assert g.startswith("[0:v]crop=w=608:h=1080:x='") and g.endswith("[v]") and "scale=1080:1920" in g


def test_a_facetrack_layout_without_a_path_is_an_error_not_a_silent_centre_crop():
    with pytest.raises(lo.UnsupportedLayout, match="path"):
        lo.filter_graph(cs.Layout(type="facetrack"), src_w=SRC_W, src_h=SRC_H, out_w=1080, out_h=1920)


def test_the_crop_is_as_wide_as_a_9_16_column_of_the_source_height():
    assert ft.crop_width(1920, 1080, 1080, 1920) == 608
    assert ft.crop_width(1280, 720, 1080, 1920) == 404
    assert ft.crop_width(300, 1080, 1080, 1920) == 300  # a source narrower than the column is not stretched


FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


@needs_ffmpeg
@pytest.mark.skipif(not FONT.exists(), reason="needs DejaVu Sans Bold")
def test_a_podcast_clip_renders_full_vertical_and_asks_for_the_faces_of_its_own_stretch(tmp_path, monkeypatch):
    from services.render import ffmpeg as ff

    source = ramp_video(tmp_path / "ramp.mp4")
    base = next(p for p in cs.load_profile(cs.ROOT / "profiles" / "podcast_id.yaml"))
    spec = base.model_dump(mode="json")
    spec["captions"]["font"] = str(FONT)
    spec["branding"]["logo"]["enabled"] = False
    profile = cs.Profile.model_validate(spec)
    assert profile.layout.type == "facetrack"
    asked: list[tuple[float, float]] = []

    def fake_detect(src, start_s, end_s, **kwargs):
        asked.append((start_s, end_s))
        return [[ft.Face(cx=0.3 + 0.4 * i / 15, w=0.1, score=0.9)] for i in range(16)]

    monkeypatch.setattr(ft, "detect", fake_detect)
    out = ff.render_clip(profile, source, 1.0, 5.0, tmp_path / "clip.mp4")
    info = ff.probe(out)
    assert (info.width, info.height) == (1080, 1920) and info.duration_s == pytest.approx(4.0, abs=0.3)
    assert asked == [(1.0, 5.0)]
