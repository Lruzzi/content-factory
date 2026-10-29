"""Two extras of the split layout: the donation alert shown above the gameplay, and the pan across full-screen menus."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import config_schema as cs

BOX = {"x": 1190, "y": 105, "w": 530, "h": 315}
SPLIT = {
    "type": "split",
    "cam": {"x": 0, "y": 758, "w": 572, "h": 322},
    "main": {"x": 420, "y": 0, "w": 1080, "h": 1080},
}


def test_both_extras_are_off_unless_asked_for():
    layout = cs.Layout(**SPLIT)
    assert layout.donation is None and layout.wide is None


def test_the_donation_box_and_the_wide_pan_have_safe_defaults():
    layout = cs.Layout(**SPLIT, donation=BOX, wide={})
    assert layout.donation == cs.Box(**BOX) and layout.donation_width == 0.72 and layout.donation_min_share == 0.0005
    assert layout.wide is not None and (layout.wide.enabled, layout.wide.green_below, layout.wide.pan) == (
        True, 0.10, "left_to_right",
    )  # fmt: skip


@pytest.mark.parametrize(
    "bad",
    [
        {"donation_width": 0},
        {"donation_width": 1.5},
        {"donation_min_share": -1},
        {"wide": {"green_below": 2}},
        {"wide": {"pan": "up"}},
    ],
)
def test_nonsense_values_are_refused(bad):
    with pytest.raises(ValidationError):
        cs.Layout(**SPLIT, **bad)


@pytest.mark.parametrize("kind", ["center", "blur_fill", "facetrack"])
def test_the_donation_card_works_on_every_layout_and_only_the_menu_scenes_belong_to_split(kind):
    assert cs.Layout(type=kind, donation=BOX).donation.model_dump() == BOX
    with pytest.raises(ValidationError, match="split"):
        cs.Layout(type=kind, wide={})


def test_where_the_card_goes_on_a_layout_other_than_split_is_a_share_of_the_height():
    assert cs.Layout(type="center").donation_y == 0.18
    assert cs.Layout(type="center", donation=BOX, donation_y=0.3).donation_y == 0.3
    for bad in (-0.1, 0.9):
        with pytest.raises(ValidationError):
            cs.Layout(type="center", donation=BOX, donation_y=bad)


def mlbb(source_id):
    return next(p for p in cs.load_profile(cs.ROOT / "profiles" / "mlbb.yaml") if p.source.id == source_id)


@pytest.mark.parametrize("source_id", ["leokabuki", "jejeadriel20"])
def test_both_mlbb_creators_have_a_measured_donation_box_and_the_menu_pan(source_id):
    layout = mlbb(source_id).layout
    assert layout.donation is not None and layout.wide is not None and layout.wide.enabled
    assert layout.donation.w > 0 and layout.donation.h > 0


def test_a_menu_is_shown_whole_by_default_and_the_pan_is_a_setting():
    assert cs.Wide().mode == "fit" and cs.Wide(mode="pan").mode == "pan"
    with pytest.raises(ValueError, match="mode"):
        cs.Wide(mode="spin")
