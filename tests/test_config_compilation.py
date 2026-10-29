"""The one-match compilation is configured per profile, off unless asked for, and only possible when the full transcript
and the LLM judge are there to find matches and pick moments."""

from __future__ import annotations

import pytest

import config_schema as cs
from profile_factory import build, write_profile

ON = {"enabled": True}


def test_a_profile_that_says_nothing_has_no_compilation():
    p = cs.load_profile(cs.ROOT / "profiles" / "podcast_id.yaml")[0]
    assert p.compilation.enabled is False


def test_the_mlbb_profile_turns_it_on_with_lengths_every_platform_accepts():
    p = next(x for x in cs.load_profile(cs.ROOT / "profiles" / "mlbb.yaml") if x.source.id == "leokabuki")
    c = p.compilation
    assert c.enabled and c.length_s[1] <= 180  # YouTube Shorts allow three minutes
    assert c.moment_s[0] < c.moment_s[1] and c.min_moments <= c.max_moments


def test_the_defaults_are_sensible(tmp_path):
    c = build(tmp_path).compilation
    assert (c.match_window_min, c.min_match_s, c.length_s, c.moment_s) == (45, 480, (60, 170), (8, 35))
    assert (c.min_moments, c.max_moments) == (3, 8)


@pytest.mark.parametrize(
    "change,message",
    [
        ({"length_s": [100, 100]}, "length_s"),
        ({"length_s": [60, 400]}, "length_s"),
        ({"length_s": [5, 60]}, "length_s"),
        ({"moment_s": [30, 10]}, "moment_s"),
        ({"moment_s": [8, 200]}, "moment_s"),
        ({"min_moments": 9, "max_moments": 8}, "min_moments"),
        (
            {"min_moments": 1, "max_moments": 2, "moment_s": [8, 20], "length_s": [60, 170]},
            "reach",
        ),  # 2 x 20 s can never make 60 s
        ({"min_moments": 6, "moment_s": [40, 60], "length_s": [60, 100]}, "fit"),  # 6 x 40 s cannot stay under 100 s
        ({"min_match_s": 10}, "min_match_s"),
        ({"match_window_min": 5}, "match_window_min"),
    ],
)
def test_a_compilation_that_cannot_work_is_rejected_with_a_reason(tmp_path, change, message):
    with pytest.raises(ValueError, match=message):
        build(tmp_path, compilation=ON | change)


def test_it_needs_the_full_transcript_and_the_llm_judge(tmp_path):
    transcript_off = {"transcript": {"enabled": True, "mode": "candidates_only"}}
    with pytest.raises(ValueError, match="compilation"):
        build(tmp_path, compilation=ON, signals=transcript_off)
    with pytest.raises(ValueError, match="compilation"):
        build(tmp_path, compilation=ON, selection={"llm_judge": {"enabled": False}})


def test_it_can_be_switched_on_for_one_source_only(tmp_path):
    profiles = cs.load_profile(
        write_profile(
            tmp_path,
            signals={"transcript": {"enabled": True, "mode": "full"}},
            sources=[
                {"url": "https://www.youtube.com/@one", "overrides": {"compilation": {"enabled": True}}},
                {"url": "https://www.youtube.com/@two"},
            ],
        )
    )
    assert [p.compilation.enabled for p in profiles] == [True, False]
