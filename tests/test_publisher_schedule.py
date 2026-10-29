"""When a clip may be posted to an account: posting windows, the daily cap and the spacing between posts (FR-9)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from services.publisher import schedule

JAKARTA = ZoneInfo("Asia/Jakarta")
WINDOWS = ["11:00-13:00", "18:00-22:00"]


def at(hour, minute=0, day=23):
    return datetime(2026, 10, day, hour, minute, tzinfo=JAKARTA)


def check(now, posts=(), cap=3, spacing_min=120, windows=WINDOWS):
    return schedule.check(now=now, posts=list(posts), cap=cap, spacing_min=spacing_min, windows=windows, tz=JAKARTA)


def test_inside_a_posting_window_with_room_left_is_allowed():
    assert check(at(19)) is None and check(at(12, 30)) is None


def test_outside_every_window_is_not_allowed_and_says_so():
    assert "window" in check(at(15))
    assert "window" in check(at(8))


def test_a_window_includes_its_start_and_not_its_end():
    assert check(at(18)) is None
    assert "window" in check(at(22))
    assert check(at(21, 59)) is None


def test_a_window_may_run_past_midnight():
    wrap = ["22:00-02:00"]
    assert check(at(23), windows=wrap) is None and check(at(1), windows=wrap) is None
    assert "window" in check(at(3), windows=wrap)


def test_no_windows_means_any_time():
    assert check(at(4), windows=[]) is None


def test_the_daily_cap_stops_further_posts_that_day():
    three = [at(11, 5), at(18, 5), at(20, 0)]
    assert "cap" in check(at(21), three, cap=3, spacing_min=0)
    assert check(at(21), three[:2], cap=3, spacing_min=0) is None


def test_a_post_stops_counting_toward_the_cap_24_hours_later():
    posts = [at(18, 5, day=22), at(19, 0, day=22), at(20, 0, day=22)]
    assert "cap" in check(at(18, 0), posts, cap=3, spacing_min=0, windows=[])  # all three are inside the last 24 h
    assert check(at(18, 30), posts, cap=3, spacing_min=0, windows=[]) is None  # the first has gone out of it
    assert check(at(18, 5), [at(18, 5, day=22)] * 3, cap=3, spacing_min=0, windows=[]) is None  # exactly 24 h: out


def test_the_cap_does_not_reset_at_midnight():
    """The calendar day would let 3 posts go at 23:30 and 3 more at 00:05; a platform counting 24 hours sees 6."""
    utc = ZoneInfo("UTC")
    posts = [datetime(2026, 10, 22, 16, 30, tzinfo=utc)] * 3  # 23:30 on 22 Oct in Jakarta
    after_midnight = datetime(2026, 10, 22, 17, 5, tzinfo=utc)  # 00:05 on 23 Oct in Jakarta
    assert "cap" in check(after_midnight, posts, cap=3, spacing_min=0, windows=[])
    next_evening = datetime(2026, 10, 23, 16, 31, tzinfo=utc)
    assert check(next_evening, posts, cap=3, spacing_min=0, windows=[]) is None


def test_posts_must_be_spaced_apart():
    assert "spacing" in check(at(19, 0), [at(18, 30)], spacing_min=120)
    assert check(at(20, 30), [at(18, 30)], spacing_min=120) is None  # exactly 120 minutes later
    assert "spacing" in check(at(20, 29), [at(18, 30)], spacing_min=120)


def test_the_most_recent_post_counts_for_spacing_whatever_the_order_given():
    assert "spacing" in check(at(19, 30), [at(19, 0), at(11, 0)], spacing_min=60)


@pytest.mark.parametrize("bad", ["", "11-13", "25:00-26:00", "11:00-13:00-14:00", "abc", "13:00-13:00"])
def test_a_window_that_cannot_be_read_is_an_error(bad):
    with pytest.raises(ValueError, match="window"):
        check(at(12), windows=[bad])


def test_a_naive_time_is_refused_because_it_would_be_a_guess():
    with pytest.raises(ValueError, match="time zone"):
        check(datetime(2026, 10, 23, 12, 0))


def test_times_given_in_another_zone_are_converted_to_the_accounts_zone_first():
    utc = ZoneInfo("UTC")
    assert check(datetime(2026, 10, 23, 12, 0, tzinfo=utc)) is None  # 19:00 in Jakarta: inside 18:00-22:00
    assert "window" in check(datetime(2026, 10, 23, 8, 0, tzinfo=utc))  # 15:00 in Jakarta
