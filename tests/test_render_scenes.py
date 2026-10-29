"""Full-screen menus (draft pick, loading screen) look nothing like the game: no green. Spotting them, and what the
render then does: the gameplay panel pans across the whole width instead of showing the middle square."""

from __future__ import annotations

import numpy as np
import pytest

from services.render import scenes


def frame(rgb, size=(36, 64)):
    out = np.zeros((*size, 3), dtype=np.uint8)
    out[:] = rgb
    return out


GRASS, MENU_BLUE = (60, 150, 60), (30, 50, 170)


def test_a_grassy_frame_is_mostly_green_and_a_blue_menu_is_not():
    assert scenes.green_share(frame(GRASS)) > 0.9
    assert scenes.green_share(frame(MENU_BLUE)) < 0.05


def test_the_bottom_strip_with_the_face_cam_and_overlays_is_not_looked_at():
    f = frame(MENU_BLUE)
    f[int(36 * 0.8) :, :] = GRASS  # a green overlay down at the bottom must not make a menu look like the game
    assert scenes.green_share(f) < 0.05


def test_runs_of_menu_frames_become_ranges_on_the_clips_own_clock():
    flags = [False] * 4 + [True] * 8 + [False] * 4  # at 2 frames a second: a menu from 2.0 s to 6.0 s
    assert scenes.ranges_from_flags(flags, fps=2.0, min_len_s=2.0, merge_gap_s=1.0) == [(2.0, 6.0)]


def test_a_short_blip_is_not_a_range_and_a_short_gap_does_not_split_one():
    blip = [False] * 6 + [True] * 2 + [False] * 6
    assert scenes.ranges_from_flags(blip, fps=2.0, min_len_s=2.0, merge_gap_s=1.0) == []
    gap = [True] * 6 + [False] * 1 + [True] * 6
    assert scenes.ranges_from_flags(gap, fps=2.0, min_len_s=2.0, merge_gap_s=1.0) == [(0.0, 6.5)]


def test_a_menu_that_runs_to_the_end_of_the_clip_is_a_range_to_the_end():
    assert scenes.ranges_from_flags([False] * 2 + [True] * 6, fps=2.0, min_len_s=1.0, merge_gap_s=1.0) == [(1.0, 4.0)]


def test_nothing_in_nothing_out():
    assert scenes.ranges_from_flags([], fps=2.0, min_len_s=1.0, merge_gap_s=1.0) == []
    assert scenes.ranges_from_flags([False] * 9, fps=2.0, min_len_s=1.0, merge_gap_s=1.0) == []


def alert_frame(yellow_pixels: int, size=(60, 100)):
    out = np.zeros((*size, 3), dtype=np.uint8)
    out[:] = (40, 120, 60)  # scenery
    out.reshape(-1, 3)[:yellow_pixels] = (255, 250, 20)  # bright yellow message text
    return out


def test_the_yellow_text_of_a_donation_alert_is_counted_and_scenery_is_not():
    assert scenes.yellow_share(alert_frame(0)) == 0.0
    assert scenes.yellow_share(alert_frame(300)) == pytest.approx(300 / 6000)


def test_gold_and_orange_are_not_the_alerts_yellow():
    f = np.zeros((10, 10, 3), dtype=np.uint8)
    f[:] = (220, 150, 30)  # a gold effect in the game
    assert scenes.yellow_share(f) == 0.0


def test_the_pan_goes_from_one_edge_to_the_other_over_each_range_and_holds_between():
    expr = scenes.pan_expression([(2.0, 6.0)], left=0.0, right=800.0, direction="left_to_right")
    assert "t-2" in expr and "800" in expr
    reverse = scenes.pan_expression([(2.0, 6.0)], left=0.0, right=800.0, direction="right_to_left")
    assert reverse != expr


def evaluate(expr, t):
    """Evaluate an FFmpeg-style expression of t in Python (only the functions the pan uses)."""
    env = {
        "t": t, "lt": lambda a, b: 1.0 if a < b else 0.0, "min": min, "max": max,
        "if": lambda c, a, b: a if c else b,
    }  # fmt: skip
    return eval(expr.replace("if(", "if_(").replace(",", ", "), {"if_": env["if"], **env})  # noqa: S307  # test-only


def test_the_pan_moves_left_to_right_through_the_range_and_right_to_left_the_other_way():
    ltr = scenes.pan_expression([(2.0, 6.0)], left=0.0, right=800.0, direction="left_to_right")
    assert evaluate(ltr, 2.0) == pytest.approx(0.0) and evaluate(ltr, 4.0) == pytest.approx(400.0)
    assert evaluate(ltr, 6.0) == pytest.approx(800.0)
    rtl = scenes.pan_expression([(2.0, 6.0)], left=0.0, right=800.0, direction="right_to_left")
    assert evaluate(rtl, 2.0) == pytest.approx(800.0) and evaluate(rtl, 6.0) == pytest.approx(0.0)


def test_each_range_pans_by_itself():
    expr = scenes.pan_expression([(1.0, 3.0), (5.0, 9.0)], left=0.0, right=800.0, direction="left_to_right")
    assert evaluate(expr, 1.0) == pytest.approx(0.0) and evaluate(expr, 3.0) == pytest.approx(800.0)
    assert evaluate(expr, 5.0) == pytest.approx(0.0) and evaluate(expr, 7.0) == pytest.approx(400.0)


# ---------- the filter graph ---------------------------------------------------------------------------------------
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import config_schema as cs  # noqa: E402
from profile_factory import build  # noqa: E402
from services.render import ffmpeg as ff  # noqa: E402
from services.render import layout as lo  # noqa: E402

CAM = cs.Box(x=0, y=723, w=420, h=357)
MAIN = cs.Box(x=420, y=0, w=1080, h=1080)
DON = cs.Box(x=1200, y=100, w=500, h=300)


def split_layout(**extra):
    return cs.Layout(type="split", cam=CAM, main=MAIN, split_ratio=0.40, **extra)


def graph(layout, overlays=None, src_w=1920, src_h=1080):
    return lo.filter_graph(layout, src_w=src_w, src_h=src_h, out_w=1080, out_h=1920, overlays=overlays)


def test_without_extras_the_split_graph_is_unchanged():
    plain = graph(split_layout())
    assert plain == graph(split_layout(donation=DON, wide=cs.Wide()), lo.Overlays())  # nothing asked for, nothing added
    assert "overlay" not in plain and "split=2" in plain


def test_a_menu_range_adds_a_panning_copy_of_the_gameplay_panel_shown_only_during_the_range():
    g = graph(split_layout(wide=cs.Wide(mode="pan")), lo.Overlays(wide=[(2.0, 6.0)]))
    assert "split=3" in g and "enable='between(t,2,6)'" in g
    assert "crop=w=" in g and "x='" in g and g.endswith("[v]")


def test_the_default_for_a_menu_is_the_whole_frame_scaled_to_the_width_over_a_blurred_copy_not_a_pan():
    g = graph(split_layout(wide=cs.Wide()), lo.Overlays(wide=[(2.0, 6.0)]))
    assert "enable='between(t,2,6)'" in g and "split=3" in g
    assert "scale=1080:-2" in g  # the whole width of the frame, whatever its height comes to
    assert "boxblur" in g and "overlay=0:(H-h)/2" in g  # centred over a blurred copy that fills the panel
    assert "x='" not in g and "crop=w=" not in g  # nothing moves


def test_fit_also_works_on_a_frame_as_narrow_as_the_pan_window():
    narrow = cs.Layout(
        type="split", cam=cs.Box(x=0, y=723, w=300, h=357), main=cs.Box(x=0, y=0, w=900, h=1080), split_ratio=0.40,
        wide=cs.Wide(),
    )  # fmt: skip
    assert "enable='between(t,2,6)'" in graph(narrow, lo.Overlays(wide=[(2.0, 6.0)]), src_w=960, src_h=1080)


def test_two_ranges_are_enabled_together():
    g = graph(split_layout(wide=cs.Wide()), lo.Overlays(wide=[(1.0, 3.0), (5.0, 9.0)]))
    assert "between(t,1,3)+between(t,5,9)" in g


def test_a_donation_clip_adds_the_alert_under_the_captions_in_the_middle_while_it_is_on_screen():
    g = graph(split_layout(donation=DON), lo.Overlays(donation=[(1.0, 9.0)]))
    assert (
        "split=3" in g and "overlay=x=(W-w)/2:y=908:enable='between(t,1,9)'" in g
    )  # 768 px cam panel, then the 140 px the captions sit in
    assert "crop=500:300:1200:100" in g and "scale=778:-2" in g  # the box as measured, 72% of 1080 wide


def test_both_extras_together_split_the_source_four_ways():
    both = lo.Overlays(wide=[(2.0, 6.0)], donation=[(1.0, 3.0)])
    fit = graph(split_layout(donation=DON, wide=cs.Wide()), both)
    pan = graph(split_layout(donation=DON, wide=cs.Wide(mode="pan")), both)
    assert "split=4" in fit and "split=4" in pan
    assert fit.count("overlay=") == 3 and pan.count("overlay=") == 2  # fit also centres the frame over its blurred copy


def test_a_source_as_narrow_as_the_pan_window_gets_no_pan():
    narrow = cs.Layout(
        type="split", cam=cs.Box(x=0, y=723, w=300, h=357), main=cs.Box(x=0, y=0, w=900, h=1080), split_ratio=0.40,
        wide=cs.Wide(mode="pan"),
    )  # fmt: skip
    g = graph(narrow, lo.Overlays(wide=[(2.0, 6.0)]), src_w=960, src_h=1080)  # a window 1012 wide in a 960-wide frame
    assert "enable=" not in g


def test_the_extras_without_their_config_are_an_error_rather_than_a_silent_skip():
    with pytest.raises(ValueError, match="donation"):
        graph(split_layout(), lo.Overlays(donation=[(1.0, 3.0)]))
    with pytest.raises(ValueError, match="wide"):
        graph(split_layout(), lo.Overlays(wide=[(1.0, 3.0)]))


# ---------- a real render -------------------------------------------------------------------------
needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="no ffmpeg")
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
PLAIN = {
    "captions": {"enabled": False},
    "branding": {"logo": {"enabled": False}, "credit": {"text": None}, "hook_overlay": {"enabled": False}},
}


def menu_video(path: Path) -> Path:
    """960x540: 3 s of grass, then 4 s of a menu whose red grows left to right; a red box sits where the alert is."""
    subprocess.run(  # noqa: S603  # nosec B603
        [
            FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=0x3c963c:s=960x540:r=25:d=3",
            "-f", "lavfi", "-i", "color=c=0x1e32aa:s=960x540:r=25:d=4",
            "-filter_complex",
            "[1:v]geq=r='30+200*X/W':g='50':b='170'[menu];[0:v][menu]concat=n=2:v=1:a=0,"
            "drawbox=x=600:y=50:w=250:h=150:color=red:t=fill:enable='between(t,0.3,3.0)',"
            "drawbox=x=620:y=160:w=60:h=10:color=yellow:t=fill:enable='between(t,0.3,3.0)'[v]",
            "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, timeout=120,
    )  # fmt: skip
    return path


def pixel(video: Path, at_s: float, x: int, y: int) -> tuple[int, int, int]:
    done = subprocess.run(  # noqa: S603  # nosec B603
        [
            FFMPEG, "-v", "error", "-ss", f"{at_s}", "-i", str(video), "-frames:v", "1",
            "-vf", f"crop=1:1:{x}:{y},format=rgb24", "-f", "rawvideo", "-",
        ],
        capture_output=True, check=True, timeout=60,
    )  # fmt: skip
    return done.stdout[0], done.stdout[1], done.stdout[2]


def profile(tmp_path, **layout):
    return build(
        tmp_path,
        layout={"type": "split", "cam": CAM.model_dump(), "main": MAIN.model_dump(), "split_ratio": 0.40, **layout},
        **PLAIN,
    )


@needs_ffmpeg
def test_a_menu_in_the_clip_makes_the_gameplay_panel_pan_while_the_game_itself_does_not(tmp_path):
    source = menu_video(tmp_path / "menu.mp4")
    out = ff.render_clip(profile(tmp_path, wide={"mode": "pan"}), source, 0.0, 7.0, tmp_path / "clip.mp4")
    panel_y = 768 + 400
    in_game = pixel(out, 1.0, 4, panel_y)
    menu_start, menu_end = pixel(out, 3.4, 4, panel_y), pixel(out, 6.6, 4, panel_y)
    assert in_game[1] > 120 and in_game[2] < 100  # grass in the game
    assert menu_start[2] > 140 and menu_end[2] > 140  # the menu (blue)
    assert menu_end[0] > menu_start[0] + 40  # the window's left edge moved right: more red at the left of the panel


@needs_ffmpeg
def test_with_the_pan_switched_off_the_menu_is_just_the_middle_square(tmp_path):
    source = menu_video(tmp_path / "menu.mp4")
    out = ff.render_clip(profile(tmp_path, wide={"enabled": False}), source, 0.0, 7.0, tmp_path / "clip.mp4")
    a, b = pixel(out, 3.4, 4, 768 + 400), pixel(out, 6.6, 4, 768 + 400)
    assert abs(a[0] - b[0]) < 12  # nothing moves


@needs_ffmpeg
def test_yellow_text_higher_up_in_the_box_is_game_text_not_an_alert(tmp_path):
    source = menu_video(tmp_path / "menu.mp4")
    high = (600, 0, 250, 100)  # a box whose lower part is the red card, not the text line at y=160: the text is outside
    assert scenes.alert_ranges(source, 0.0, 3.0, box=high, min_share=0.0005) == []


def test_a_donation_clip_shows_the_alert_at_the_top_middle_of_the_gameplay_panel_only_while_it_is_on_screen(tmp_path):
    source = menu_video(tmp_path / "menu.mp4")
    with_box = profile(tmp_path, donation={"x": 1200, "y": 100, "w": 500, "h": 300})
    shown = ff.render_clip(with_box, source, 0.0, 5.0, tmp_path / "don.mp4", kind="donation")
    other = ff.render_clip(with_box, source, 0.0, 5.0, tmp_path / "other.mp4", kind="highlight")
    centre = (540, 768 + 24 + 200)
    red = lambda p: p[0] > 200 and p[1] < 60 and p[2] < 60  # noqa: E731

    assert red(pixel(shown, 1.0, *centre))  # the alert is up: its red card is copied over the game
    assert not red(pixel(shown, 4.0, *centre))  # it is gone: no patch of scenery is left behind
    assert not red(pixel(other, 1.0, *centre))  # and only a donation clip gets it at all


@needs_ffmpeg
def test_the_alert_ranges_are_found_from_the_yellow_text_in_the_box(tmp_path):
    source = menu_video(tmp_path / "menu.mp4")
    found = scenes.alert_ranges(
        source, 0.0, 3.0, box=(600, 50, 250, 150), min_share=0.0002
    )  # the box, in source pixels
    assert len(found) == 1 and found[0][0] == pytest.approx(0.3, abs=0.3) and found[0][1] == pytest.approx(3.0, abs=0.5)


@needs_ffmpeg
def test_a_menu_in_the_clip_shows_the_whole_frame_still_over_a_blurred_fill_not_a_pan_and_not_black(tmp_path):
    source = menu_video(tmp_path / "menu.mp4")
    out = ff.render_clip(profile(tmp_path, wide={}), source, 0.0, 7.0, tmp_path / "clip.mp4")
    mid = 768 + 576  # the middle of the gameplay panel: the whole frame sits here
    early, late = pixel(out, 3.4, 4, mid), pixel(out, 6.6, 4, mid)
    assert abs(early[0] - late[0]) < 12 and abs(early[2] - late[2]) < 12  # nothing moves during the menu
    top = pixel(out, 5.0, 540, 768 + 8)  # above the 16:9 band: the blurred fill, never a black bar
    assert sum(top) > 120


# ---------- a sharper way to enlarge the face cam ----------
def test_by_default_the_panels_are_scaled_as_before_with_no_sharpening():
    g = graph(split_layout())
    assert "lanczos" not in g and "unsharp" not in g


def test_the_lanczos_scaler_applies_to_both_panels():
    g = graph(split_layout(scaler="lanczos"))
    assert g.count("flags=lanczos") == 2 and "unsharp" not in g


def test_sharpening_applies_only_to_the_enlarged_cam_panel():
    g = graph(split_layout(sharpen=0.8))
    cam, main = [part for part in g.split(";") if part.endswith(("[cam]", "[main]"))]
    assert "unsharp=5:5:0.8" in cam and "unsharp" not in main


@pytest.mark.parametrize("bad", [{"sharpen": -0.1}, {"sharpen": 3}, {"scaler": "nearest"}])
def test_a_sharpen_amount_or_scaler_outside_the_known_ones_is_refused(bad):
    with pytest.raises(ValueError):
        split_layout(**bad)


# ---------- the donation card on every layout, not only split ----------
@pytest.mark.parametrize("kind", ["center", "blur_fill"])
def test_a_donation_clip_of_any_layout_gets_the_card_centred_while_the_alert_is_up(kind):
    layout = cs.Layout(type=kind, donation=DON)
    g = lo.filter_graph(
        layout, src_w=1920, src_h=1080, out_w=1080, out_h=1920, overlays=lo.Overlays(donation=[(1.0, 3.0)])
    )
    assert g.startswith("[0:v]split=2[src][dsrc];") and g.endswith("[v]")
    assert "crop=500:300:1200:100" in g and "scale=778:-2[don]" in g  # the box as measured, 72% of the width
    assert "overlay=x=(W-w)/2:y=346:enable='between(t,1,3)'" in g  # 18% of the height, centred


def test_the_card_on_a_facetrack_clip_leaves_the_face_following_untouched():
    knots = [(0.0, 400.0), (5.0, 600.0)]
    layout = cs.Layout(type="facetrack", donation=DON)
    plain = lo.filter_graph(layout, src_w=1920, src_h=1080, out_w=1080, out_h=1920, track=knots)
    with_card = lo.filter_graph(
        layout, src_w=1920, src_h=1080, out_w=1080, out_h=1920, track=knots, overlays=lo.Overlays(donation=[(1.0, 3.0)])
    )
    assert plain.replace("[0:v]", "[src]", 1).replace("[v]", "[vbase]") in with_card


def test_without_an_alert_every_layout_graph_is_what_it_was():
    for kind in ("center", "blur_fill"):
        layout = cs.Layout(type=kind, donation=DON)
        a = lo.filter_graph(layout, src_w=1920, src_h=1080, out_w=1080, out_h=1920)
        b = lo.filter_graph(layout, src_w=1920, src_h=1080, out_w=1080, out_h=1920, overlays=lo.Overlays())
        assert a == b and "dsrc" not in a


def test_an_alert_without_a_measured_box_is_an_error_on_every_layout():
    with pytest.raises(ValueError, match="layout.donation"):
        lo.filter_graph(
            cs.Layout(type="center"),
            src_w=1920,
            src_h=1080,
            out_w=1080,
            out_h=1920,
            overlays=lo.Overlays(donation=[(1.0, 3.0)]),
        )


@needs_ffmpeg
def test_a_donation_clip_on_a_centre_layout_shows_the_card_only_while_the_alert_is_up(tmp_path):
    source = menu_video(tmp_path / "menu.mp4")
    plain = build(tmp_path, layout={"type": "center", "donation": {"x": 1200, "y": 100, "w": 500, "h": 300}}, **PLAIN)
    shown = ff.render_clip(plain, source, 0.0, 5.0, tmp_path / "don.mp4", kind="donation")
    other = ff.render_clip(plain, source, 0.0, 5.0, tmp_path / "other.mp4", kind="highlight")
    centre = (540, 346 + 230)  # the middle of the card, 18% down
    red = lambda p: p[0] > 200 and p[1] < 60 and p[2] < 60  # noqa: E731
    assert (
        red(pixel(shown, 1.0, *centre)) and not red(pixel(shown, 4.0, *centre)) and not red(pixel(other, 1.0, *centre))
    )
