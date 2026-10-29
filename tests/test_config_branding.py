"""Branding settings are checked when the config loads, not when a clip fails to render (FR-1, FR-6)."""

from __future__ import annotations

import pytest

from profile_factory import build


def test_the_default_branding_is_valid(tmp_path):
    assert build(tmp_path).branding["logo"]["position"] == "top_right"


@pytest.mark.parametrize("position", ["top_right", "top_left", "bottom_right", "bottom_left"])
def test_every_corner_is_a_valid_logo_position(tmp_path, position):
    assert build(tmp_path, branding={"logo": {"position": position}}).branding["logo"]["position"] == position


def test_an_unknown_logo_position_is_rejected(tmp_path):
    with pytest.raises(ValueError, match=r"branding\.logo\.position"):
        build(tmp_path, branding={"logo": {"position": "middle"}})


@pytest.mark.parametrize("opacity", [-0.1, 1.5, "half"])
def test_logo_opacity_must_be_between_0_and_1(tmp_path, opacity):
    with pytest.raises(ValueError, match=r"branding\.logo\.opacity"):
        build(tmp_path, branding={"logo": {"opacity": opacity}})


@pytest.mark.parametrize("width", [0, -5, 5000, "wide"])
def test_logo_width_must_be_a_sensible_number_of_pixels(tmp_path, width):
    with pytest.raises(ValueError, match=r"branding\.logo\.width"):
        build(tmp_path, branding={"logo": {"width": width}})


def test_the_logo_can_be_switched_off(tmp_path):
    assert build(tmp_path, branding={"logo": {"enabled": False}}).branding["logo"]["enabled"] is False


@pytest.mark.parametrize("bad", [{"y": 1.5}, {"y": -0.1}, {"seconds": -1}, {"seconds": 99}])
def test_the_hook_overlay_must_fit_the_screen_and_the_clip(tmp_path, bad):
    with pytest.raises(ValueError, match=r"branding\.hook_overlay"):
        build(tmp_path, branding={"hook_overlay": bad})


def test_the_credit_can_be_left_out_with_no_text(tmp_path):
    assert build(tmp_path, branding={"credit": {"text": None}}).branding["credit"]["text"] is None


@pytest.mark.parametrize(
    "bad", [{"top": 0.7, "bottom": 0.5}, {"left": 0.6, "right": 0.5}, {"top": -0.1}, {"right": 1.2}]
)
def test_the_safe_zone_must_leave_room_for_content(tmp_path, bad):
    with pytest.raises(ValueError, match="safe_zone"):
        build(tmp_path, safe_zone=bad)
