"""Which videos of a channel to fetch, in which order, how many, and which stretch of each. Pure: no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import config_schema as cs
from services.discover import rules
from services.discover.youtube import Video

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def video(
    vid="v1", *, title="Solo rank mythic", duration=7200.0, views=1000, live="was_live", avail="public", age_days=3.0
):
    return Video(
        id=vid, title=title, duration_s=duration, views=views, live_status=live, availability=avail,
        uploaded=NOW - timedelta(days=age_days) if age_days is not None else None,
    )  # fmt: skip


def discover(**kw):
    return cs.Discover(min_duration_s=900, **kw)


# ---------- the cheap checks, on the channel's list ---------------------------------------------------------------
def test_an_ordinary_ended_stream_passes():
    assert rules.skip_reason(video(), discover(), known=set()) is None


@pytest.mark.parametrize(
    ("change", "word"),
    [
        ({"live": "is_live"}, "live"),
        ({"live": "is_upcoming"}, "live"),
        ({"duration": None}, "duration"),
        ({"duration": 600.0}, "duration"),
        ({"duration": 90000.0}, "duration"),
        ({"title": "MLBB Trailer"}, "excluded"),
    ],
)
def test_a_video_that_cannot_be_used_is_skipped_with_the_reason(change, word):
    reason = rules.skip_reason(video(**change), discover(title_exclude=["trailer"]), known=set())
    assert reason is not None and word in reason


def test_a_video_already_in_the_system_is_skipped():
    assert "already" in (rules.skip_reason(video("seen"), discover(), known={"seen"}) or "")


def test_a_title_must_contain_one_of_the_wanted_words_when_there_are_any():
    d = discover(title_include=["mlbb", "rank"])
    assert rules.skip_reason(video(title="Solo RANK epic"), d, known=set()) is None
    assert "not wanted" in (rules.skip_reason(video(title="Tanya jawab"), d, known=set()) or "")


def test_a_short_is_skipped_unless_shorts_are_wanted():
    assert rules.skip_reason(video(duration=50.0), cs.Discover(min_duration_s=10), known=set()) is not None
    assert (
        rules.skip_reason(video(duration=50.0), cs.Discover(min_duration_s=10, include_shorts=True), known=set())
        is None
    )


# ---------- the checks that need the video's own page --------------------------------------------------------------
@pytest.mark.parametrize("availability", ["subscriber_only", "premium_only", "needs_auth", "private", None])
def test_only_public_or_unlisted_videos_are_ever_fetched(availability):
    assert "not public" in (rules.detail_skip_reason(video(avail=availability), discover(), NOW) or "")


def test_a_video_older_than_the_window_is_skipped():
    d = discover(max_age_days=60)
    assert rules.detail_skip_reason(video(age_days=59), d, NOW) is None
    assert "too old" in (rules.detail_skip_reason(video(age_days=61), d, NOW) or "")


def test_a_video_with_no_known_upload_date_is_skipped_rather_than_guessed():
    assert "date" in (rules.detail_skip_reason(video(age_days=None), discover(), NOW) or "")


def test_a_stream_that_is_still_being_processed_is_skipped():
    assert "processing" in (rules.detail_skip_reason(video(live="post_live"), discover(), NOW) or "")


# ---------- the order -----------------------------------------------------------------------------
def test_a_video_from_the_last_two_days_goes_first_newest_first_then_the_rest_by_views():
    old_big, old_small = video("big", views=9000, age_days=20), video("small", views=10, age_days=15)
    new_a, new_b = video("a", views=1, age_days=1.5), video("b", views=1, age_days=0.5)
    assert [v.id for v in rules.rank([old_small, new_a, old_big, new_b], NOW)] == ["b", "a", "big", "small"]


# ---------- how many ------------------------------------------------------------------------------------------------
def test_the_first_time_a_source_gets_the_backfill_number_and_afterwards_the_daily_number():
    d = discover(backfill_per_source=5, daily_per_source=2)
    assert rules.allowance(d, ever=0, last_day=0) == 5
    assert rules.allowance(d, ever=5, last_day=5) == 0  # the first batch used today's turn too
    assert rules.allowance(d, ever=5, last_day=0) == 2
    assert rules.allowance(d, ever=5, last_day=1) == 1
    assert rules.allowance(d, ever=9, last_day=7) == 0


# ---------- which stretch -------------------------------------------------------------------------
def test_a_video_no_longer_than_the_window_is_taken_whole():
    assert rules.best_window([], 1800.0, 3600) == (0, 1800)


def test_the_busiest_stretch_of_the_most_replayed_graph_is_chosen():
    heat = [(i * 100.0, (i + 1) * 100.0, 0.9 if 30 <= i < 36 else 0.1) for i in range(100)]  # busy at 3000-3600 s
    start, length = rules.best_window(heat, 10000.0, 1200)
    assert length == 1200 and 2400 <= start <= 3000 and start % 60 == 0  # the window sits on the busy part


def test_without_a_graph_the_window_starts_a_little_in_and_always_fits():
    start, length = rules.best_window([], 18000.0, 3600)
    assert length == 3600 and 0 < start <= 18000 - 3600 and start % 60 == 0
    start, length = rules.best_window([], 3700.0, 3600)
    assert start + length <= 3700


def test_a_window_never_runs_past_the_end_of_the_video():
    heat = [(i * 100.0, (i + 1) * 100.0, 0.1 if i < 95 else 1.0) for i in range(100)]
    start, length = rules.best_window(heat, 10000.0, 1800)
    assert start + length <= 10000
