"""Putting the LLM's title and description together into the text each platform gets (FR-7). Pure code."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from services.package import compose as c

LINK = "https://www.youtube.com/watch?v=Y7iv3AoNJ0s"


def build(**kw):
    options: dict[str, Any] = {
        "title": "Donasi 10 ribu bikin heboh", "description": "Streamer kaget baca pesan donasi.",
        "generated": ["#mlbb", "#funny"], "fixed": ["#mobilelegends"], "generated_max": 3,
        "source_link": LINK, "ai_disclosure": False,
    } | kw  # fmt: skip
    return c.compose(**options)


def test_every_platform_gets_a_package_and_the_names_are_the_ones_the_accounts_use():
    assert set(build()) == {"shorts", "tiktok", "reels", "facebook"}


def test_a_short_video_platform_keeps_title_and_description_apart_and_the_others_use_one_caption():
    out = build()
    assert out["shorts"].title == "Donasi 10 ribu bikin heboh" and "Streamer kaget" in out["shorts"].text
    assert out["tiktok"].text.startswith("Donasi 10 ribu bikin heboh\n\n") and "Streamer kaget" in out["tiktok"].text


def test_fixed_hashtags_come_first_then_generated_up_to_the_limit_without_repeats():
    tags = (
        build(generated=["#MLBB", "#mobilelegends", "#funny", "#a1", "#b2"], fixed=["#mlbb"], generated_max=2)
        .get("tiktok")
        .hashtags
    )
    assert tags == ["#mlbb", "#mobilelegends", "#funny"]  # #MLBB repeats a fixed tag; two generated are kept


def test_hashtags_are_cleaned_into_plain_tags():
    tags = build(
        generated=["mlbb", "#two words", "#bad!tag", "##x", "#" + "a" * 80, "#ok_1"], fixed=[], generated_max=6
    )["reels"].hashtags
    assert tags == ["#mlbb", "#two", "#badtag", "#x", "#ok_1"]


def test_the_text_ends_with_the_hashtags_and_the_source_link_is_shown():
    text = build()["facebook"].text
    assert "Full stream: " + LINK in text and "#mlbb" in text
    assert text.index("Full stream") < text.index("#mobilelegends")


def test_without_a_source_link_none_is_shown():
    assert "Full stream" not in build(source_link=None)["tiktok"].text


def test_the_ai_disclosure_is_added_when_switched_on():
    assert "AI" in build(ai_disclosure=True)["shorts"].text and "AI" not in build()["shorts"].text


@pytest.mark.parametrize("platform", ["shorts", "tiktok", "reels", "facebook"])
def test_no_text_is_longer_than_its_platform_allows(platform):
    out = build(title="judul " * 60, description="deskripsi panjang sekali " * 400)[platform]
    assert len(out.title) <= c.LIMITS[platform].title and len(out.text) <= c.LIMITS[platform].text
    assert (
        out.hashtags and out.hashtags[0] in out.text
    )  # the hashtags survive the trimming, the description is cut instead


def test_a_title_is_cut_on_a_word_and_marked():
    title = build(title="kata " * 60)["shorts"].title
    assert title.endswith("…") and not title.endswith(" …") and len(title) <= c.LIMITS["shorts"].title


def test_control_characters_and_line_breaks_in_the_title_are_removed():
    out = build(title="satu\ndua\x00 tiga" + chr(0x202E))["shorts"]  # a right-to-left override
    assert out.title == "satu dua tiga" and "\x00" not in out.text


# ---------- the shape of the LLM's answer ---------------------------------------------------------------
def test_the_answer_needs_a_title_a_description_and_a_few_hashtags():
    a = c.Answer.model_validate({"title": "Judul", "description": "Isi", "hashtags": ["#a"]})
    assert a.title == "Judul"


@pytest.mark.parametrize(
    "bad",
    [{"title": ""}, {"title": "x" * 200}, {"description": "x" * 2000}, {"hashtags": ["#a"] * 40}, {"extra": 1}],
)
def test_an_answer_out_of_shape_is_rejected(bad):
    base = {"title": "Judul", "description": "Isi", "hashtags": ["#a"]}
    with pytest.raises(ValidationError):
        c.Answer.model_validate(base | bad)


# ---------- hashtags for one platform only ----------
def test_tiktok_gets_its_own_hashtags_after_the_fixed_ones_and_the_others_do_not():
    out = build(
        fixed=["#mlbb"], generated=["#funny"], generated_max=2, platform_fixed={"tiktok": ["#fyp", "#foryourpage"]}
    )
    assert out["tiktok"].hashtags == ["#mlbb", "#fyp", "#foryourpage", "#funny"]
    assert out["tiktok"].text.rstrip().endswith("#mlbb #fyp #foryourpage #funny")
    for platform in ("shorts", "reels", "facebook"):
        assert out[platform].hashtags == ["#mlbb", "#funny"] and "#fyp" not in out[platform].text


def test_a_platform_tag_that_is_already_there_is_not_repeated_and_none_means_as_before():
    out = build(fixed=["#fyp"], generated=["#FYP"], platform_fixed={"tiktok": ["#fyp", "#foryourpage"]})
    assert out["tiktok"].hashtags == ["#fyp", "#foryourpage"]
    assert build(platform_fixed=None)["tiktok"].hashtags == build()["tiktok"].hashtags


def test_the_repos_profiles_put_fyp_on_tiktok_captions_only():
    import config_schema as cs

    for path in ("mlbb", "podcast_id"):
        tags = cs.load_profile(cs.ROOT / "profiles" / f"{path}.yaml")[0].package["hashtags"]
        assert tags["platform_fixed"] == {"tiktok": ["#fyp", "#foryourpage"]}
