"""9:16 layouts as FFmpeg filter graphs (FR-6). Pure: no FFmpeg needed."""

from __future__ import annotations

import re
from typing import Any

import pytest

import config_schema as cs
from services.render import layout as lo

OUT: dict[str, Any] = {"out_w": 1080, "out_h": 1920}
CAM = cs.Box(x=1440, y=760, w=480, h=320)
MAIN = cs.Box(x=420, y=0, w=1080, h=1080)


def graph(layout_type, *, src_w=1920, src_h=1080, **extra):
    layout = cs.Layout(type=layout_type, **extra)
    return lo.filter_graph(layout, src_w=src_w, src_h=src_h, **OUT)


def test_every_graph_reads_the_first_video_and_names_its_output_v():
    for kind, extra in (("center", {}), ("blur_fill", {}), ("split", {"cam": CAM, "main": MAIN})):
        g = graph(kind, **extra)
        assert g.startswith("[0:v]"), kind
        assert g.endswith("[v]"), kind


def test_center_crops_a_9_16_column_from_the_middle_then_scales():
    g = graph("center")
    assert "crop=608:1080" in g  # 1080 * 9/16 = 607.5, made even
    assert "scale=1080:1920" in g


def test_center_on_a_portrait_source_does_not_crop_wider_than_the_frame():
    g = graph("center", src_w=720, src_h=1280)
    assert "crop=720:1280" in g


def test_blur_fill_blurs_a_filled_copy_behind_the_sharp_fitted_video():
    g = graph("blur_fill")
    assert "boxblur" in g
    assert "force_original_aspect_ratio=increase" in g
    assert "overlay" in g


def test_split_stacks_the_cam_panel_over_the_gameplay_panel_in_the_configured_ratio():
    g = graph("split", cam=CAM, main=MAIN, split_ratio=0.36)
    assert "vstack" in g
    assert g.index("[cam]") < g.index("[main]")  # cam on top
    # 36% of 1920 is 691.2, made even: cam panel 692 high, gameplay panel the remaining 1228
    assert "scale=1080:692" in g
    assert "scale=1080:1228" in g


def test_split_crops_the_boxes_given_for_a_1080p_source_as_they_are():
    g = graph("split", cam=CAM, main=MAIN)
    assert "crop=480:320:1440:760" in g
    assert "crop=1080:1080:420:0" in g


def test_split_scales_the_boxes_to_a_smaller_source():
    g = graph("split", src_w=1280, src_h=720, cam=CAM, main=MAIN)
    # the 1080p cam box times 2/3 is 320x213.3 at (960, 506.7): sizes made even, and the offset pulled up from 507
    # so that the box does not poke one pixel below the 720p frame
    assert "crop=320:214:960:506" in g
    assert "crop=720:720:280:0" in g


def test_split_boxes_scaled_from_the_reference_stay_on_even_pixels_for_the_sizes():
    g = graph("split", src_w=1280, src_h=720, cam=CAM, main=MAIN)
    sizes = [int(m) for crop in re.findall(r"crop=(\d+):(\d+):", g) for m in crop]
    assert all(n % 2 == 0 for n in sizes)


def test_split_needs_both_boxes():
    with pytest.raises(ValueError, match="cam"):
        graph("split", main=MAIN)
    with pytest.raises(ValueError, match="main"):
        graph("split", cam=CAM)


def test_a_box_outside_the_frame_is_rejected():
    with pytest.raises(ValueError, match="outside"):
        graph("split", cam=cs.Box(x=1800, y=760, w=480, h=320), main=MAIN)


def test_face_tracking_needs_its_path_and_says_so():
    with pytest.raises(lo.UnsupportedLayout, match="facetrack"):
        graph("facetrack")


def test_a_graph_contains_no_unescaped_commas_inside_expressions():
    # numbers are computed in Python, so the only commas are filter separators and filter options
    for kind, extra in (("center", {}), ("blur_fill", {}), ("split", {"cam": CAM, "main": MAIN})):
        assert "min(" not in graph(kind, **extra), kind


def test_a_box_that_is_more_than_a_couple_of_pixels_outside_the_frame_is_rejected_not_nudged():
    with pytest.raises(ValueError, match="outside"):
        graph("split", cam=cs.Box(x=1440, y=770, w=480, h=320), main=MAIN)


def test_other_output_sizes_are_honoured():
    g = lo.filter_graph(cs.Layout(type="center"), src_w=1920, src_h=1080, out_w=720, out_h=1280)
    assert "scale=720:1280" in g


def test_the_defaults_enlarge_with_the_sharper_scaler_and_a_light_cam_sharpen(tmp_path):
    from profile_factory import build

    layout = build(tmp_path).layout
    assert (layout.scaler, layout.sharpen, layout.donation_y) == ("lanczos", 0.8, 0.18)


def test_the_captions_of_a_split_layout_sit_under_the_cam_and_above_the_donation_card_of_every_mlbb_source():
    """Captions drawn over the card hid the donation message; below the gameplay they hid the hero."""
    profiles = cs.load_profile(cs.ROOT / "profiles" / "mlbb.yaml")
    checked = 0
    for p in profiles:
        box = p.layout.donation
        if box is None:
            continue
        cam_h = lo._even(1920 * p.layout.split_ratio)
        centre = 1920 * (p.layout.split_ratio + p.captions["below_cam"])
        half = p.captions["size"] * 1.2 / 2 + p.captions["outline"]
        card_top = cam_h + lo.DONATION_MARGIN_PX
        assert cam_h <= centre - half and centre + half < card_top, (
            p.source.id,
            centre - half,
            centre + half,
            card_top,
        )
        checked += 1
    assert checked >= 2
