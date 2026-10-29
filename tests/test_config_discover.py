"""The discover settings that keep finding old streams and uploads from eating the live capture's share of Groq."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import config_schema as cs


def mlbb():
    return next(p for p in cs.load_profile(cs.ROOT / "profiles" / "mlbb.yaml") if p.source.id == "leokabuki")


def test_discover_has_a_window_a_groq_share_and_a_backlog_limit_with_safe_defaults():
    d = cs.Discover()
    assert (d.max_window_min, d.max_groq_day_share, d.max_backlog) == (60, 0.5, 6)


@pytest.mark.parametrize(
    "bad", [{"max_window_min": 5}, {"max_groq_day_share": 1.5}, {"max_backlog": -1}, {"max_window_min": 9999}]
)
def test_nonsense_limits_are_refused(bad):
    with pytest.raises(ValidationError):
        cs.Discover(**bad)


def test_mlbb_takes_one_video_first_and_one_more_a_day():
    d = mlbb().discover
    assert (d.backfill_per_source, d.daily_per_source) == (1, 1)
    assert d.max_window_min <= 90  # one video never takes more than a share of the day's Groq audio
