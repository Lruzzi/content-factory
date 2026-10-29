"""Staying inside Groq's free limits: counting audio seconds already sent, waiting instead of failing at a limit, and
an alert before the daily limit is used up."""

from __future__ import annotations

from decimal import Decimal

import httpx2
import openai
import pytest
import sqlalchemy as sa
from media_helpers import make_clip

import config_schema as cs
from common import jobs, ledger, sources
from common.media import LocalStore
from services.understand import budget, transcribe
from services.understand import handler as understand

pytestmark = pytest.mark.integration

LIMITS = budget.Limits(audio_seconds_per_hour=7200, audio_seconds_per_day=28800, alert_at=0.8)
REPLY = {"language": "Indonesian", "duration": 20.0, "text": " halo", "segments": [], "words": []}


def spend(db, seconds, *, hours_ago=0.0, service="understand", provider="groq", unit="audio_s"):
    with db.begin() as conn:
        ledger.record_cost(
            conn, profile="mlbb", service=service, provider=provider, units=Decimal(seconds), unit=unit, usd=Decimal(0)
        )
        conn.execute(
            sa.text(
                "UPDATE costs SET created_at = clock_timestamp() - make_interval(secs => :s) "
                "WHERE id = (SELECT max(id) FROM costs)"
            ),
            {"s": hours_ago * 3600},
        )


# ---------- counting --------------------
def test_only_audio_seconds_sent_to_groq_inside_the_window_are_counted(db):
    spend(db, 100)
    spend(db, 50, hours_ago=0.5)
    spend(db, 400, hours_ago=3)  # outside the hour, inside the day
    spend(db, 900, hours_ago=30)  # outside the day
    spend(db, 77, service="select", provider="gemini", unit="input_tokens")  # not audio at all
    with db.connect() as conn:
        assert budget.used(conn, 3600) == 150 and budget.used(conn, 86400) == 550


def test_there_is_room_while_the_next_segment_fits_under_both_limits(db):
    spend(db, 7000)
    with db.connect() as conn:
        assert budget.check(conn, LIMITS, needed_s=200) is None  # exactly 7200 is still allowed


def test_a_segment_that_would_pass_the_hourly_limit_waits(db):
    spend(db, 7100)
    with db.connect() as conn:
        wait = budget.check(conn, LIMITS, needed_s=200)
    assert wait is not None and "hour" in wait.reason and 60 <= wait.wait_s <= 900


def test_a_segment_that_would_pass_the_daily_limit_waits_longer(db):
    spend(db, 28700, hours_ago=5)  # spread over the day, so the hour is fine
    with db.connect() as conn:
        wait = budget.check(conn, LIMITS, needed_s=200)
    assert wait is not None and "daily" in wait.reason and wait.wait_s >= 900


def test_a_request_shorter_than_ten_seconds_still_counts_as_ten(db):
    spend(db, 7195)
    with db.connect() as conn:
        assert budget.check(conn, LIMITS, needed_s=2) is not None  # Groq bills at least 10 s per request


def test_the_usage_report_shows_each_window_against_its_limit(db):
    spend(db, 3600)
    spend(db, 3600, hours_ago=3)
    with db.connect() as conn:
        text = budget.report(conn, LIMITS)
    assert "3600" in text and "7200" in text and "50%" in text and "7200 of 28800" in text.replace(",", "")


# ---------- the handler --------------------
class FakeGroq:
    def __init__(self, error=None):
        self.calls, self.error = 0, error

    def __call__(self, path, **kw):
        self.calls += 1
        if self.error:
            raise self.error
        return transcribe.parse(REPLY)


@pytest.fixture
def segment(db, tmp_path):
    store = LocalStore(tmp_path / "media")
    path = store.path("segments/s1/00001.ts")
    make_clip(path)
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        profile = next(p for p in profiles if p.source.id == "leokabuki")
        job_id = jobs.create_job(
            conn, profile, stage="understand", media_key="segments/s1/00001.ts",
            data={"live": True, "stream_id": None, "segment": 1, "start_s": 0.0, "end_s": 20.0},
        )  # fmt: skip
        return store, jobs.get(conn, job_id)


def handler(db, store, groq, alerts=None, limits=LIMITS):
    return understand.make_handler(
        db, store, groq, limits=limits, alert=(alerts.append if alerts is not None else None)
    )


def test_a_segment_is_not_sent_when_the_limit_would_be_passed_and_the_job_is_deferred_not_failed(db, segment):
    store, job = segment
    spend(db, 7200)
    groq = FakeGroq()
    with pytest.raises(jobs.Deferred) as caught:
        handler(db, store, groq)(job)
    assert groq.calls == 0 and "hour" in caught.value.reason and caught.value.wait_s > 0


def test_a_rate_limit_answer_from_groq_defers_the_job_for_as_long_as_groq_says(db, segment):
    store, job = segment
    with pytest.raises(jobs.Deferred) as caught:
        handler(db, store, FakeGroq(transcribe.RateLimited(42, "audio seconds per hour")))(job)
    assert caught.value.wait_s == 42 and "Groq" in caught.value.reason


def test_an_alert_is_raised_when_the_day_is_four_fifths_used_and_not_before(db, segment):
    store, job = segment
    alerts: list[str] = []
    spend(db, 20000, hours_ago=5)  # 69% of the day
    handler(db, store, FakeGroq(), alerts)(job)
    assert alerts == []
    spend(db, 3200, hours_ago=2)  # now past 80% once this segment's 20 s are added
    with db.begin() as conn:
        conn.execute(sa.text("DELETE FROM transcripts"))
    handler(db, store, FakeGroq(), alerts)(job)
    assert len(alerts) == 1 and "80%" in alerts[0] and "Groq" in alerts[0]


def test_without_limits_nothing_is_checked(db, segment):
    store, job = segment
    spend(db, 99999)
    groq = FakeGroq()
    handler(db, store, groq, limits=None)(job)
    assert groq.calls == 1


# ---------- Groq's own answer --------------------
def groq_refusal(headers):
    request = httpx2.Request("POST", "https://api.groq.com/openai/v1/audio/transcriptions")
    response = httpx2.Response(429, headers=headers, request=request, json={"error": {"message": "Rate limit reached"}})
    return openai.RateLimitError("Rate limit reached", response=response, body=None)


@pytest.mark.parametrize("headers,expected", [({"retry-after": "42"}, 42), ({}, 300), ({"retry-after": "junk"}, 300)])
def test_a_429_from_groq_becomes_a_rate_limited_error_with_the_wait_it_asked_for(tmp_path, headers, expected):
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"x")
    client = transcribe.GroqTranscriber("key-for-tests")
    refusal = groq_refusal(headers)

    def refuse(**kwargs):
        raise refusal

    client.client.audio.transcriptions.create = refuse  # type: ignore[method-assign]
    with pytest.raises(transcribe.RateLimited) as caught:
        client(audio, model="m", language="id")
    assert caught.value.wait_s == expected


# ---------- the stop at 90% --------------------------------------------------------------------------
STOPPING = budget.Limits(audio_seconds_per_hour=7200, audio_seconds_per_day=28800, alert_at=0.8, stop_at=0.9)


def test_without_a_stop_threshold_nothing_changes_and_the_old_limits_still_apply(db):
    assert LIMITS.stop_at == 1.0
    spend(db, 28000, hours_ago=5)
    with db.connect() as conn:
        assert budget.check(conn, LIMITS, needed_s=200) is None


def test_a_segment_that_would_take_the_day_past_the_stop_share_waits_though_the_limit_is_not_reached(db):
    spend(db, 25800, hours_ago=5)  # 89.6% of the day, under the 90% stop
    with db.connect() as conn:
        assert budget.check(conn, STOPPING, needed_s=10) is None  # 25810: still under 25920
        wait = budget.check(conn, STOPPING, needed_s=200)  # 26000: over 90%
    assert wait is not None and "90%" in wait.reason and wait.wait_s >= 900


def test_the_stop_is_lifted_by_itself_when_old_usage_falls_out_of_the_day(db):
    spend(db, 26500, hours_ago=25)  # more than a day ago: no longer counted
    with db.connect() as conn:
        assert budget.check(conn, STOPPING, needed_s=200) is None


def test_the_usage_report_says_where_transcription_stops(db):
    with db.connect() as conn:
        assert "90%" in budget.report(conn, STOPPING)


def test_at_the_stop_the_job_waits_and_you_are_told_once_not_for_every_segment(db, segment):
    store, job = segment
    alerts: list[str] = []
    spend(db, 26500, hours_ago=5)  # 92%
    groq = FakeGroq()
    for _ in range(3):
        with pytest.raises(jobs.Deferred):
            handler(db, store, groq, alerts, limits=STOPPING)(job)
    assert groq.calls == 0 and len(alerts) == 1 and "pause" in alerts[0].lower() and "90%" in alerts[0]


def test_a_stop_that_comes_back_the_same_day_is_not_announced_again(db, segment):
    store, job = segment
    alerts: list[str] = []
    spend(db, 26500, hours_ago=5)
    with pytest.raises(jobs.Deferred):
        handler(db, store, FakeGroq(), alerts, limits=STOPPING)(job)
    with db.begin() as conn:
        conn.execute(sa.text("UPDATE pauses SET until = clock_timestamp() - interval '1 minute'"))
    with pytest.raises(jobs.Deferred):
        handler(db, store, FakeGroq(), alerts, limits=STOPPING)(job)
    assert len(alerts) == 1
