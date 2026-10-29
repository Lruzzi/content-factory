"""Discover: when a source is not live, queue its best ended stream or upload for download, within its limits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import jobs, ledger, sources
from services.discover import tick as dt
from services.discover.youtube import DiscoverError, Video
from services.understand import budget

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
LEO = "https://www.youtube.com/@leokabuki"
JEJE = "https://www.youtube.com/@JejeAdriel20"
LIMITS = budget.Limits(audio_seconds_per_hour=7200, audio_seconds_per_day=28800, alert_at=0.8)


def vid(n: int, *, views=1000, title="Solo rank mythic", duration=7200.0, avail="public", age=3.0, heat=()):
    return Video(
        id=f"video{n:06d}", title=title, duration_s=duration, views=views, live_status="was_live", availability=avail,
        uploaded=NOW - timedelta(days=age), heatmap=heat,
    )  # fmt: skip


class Channel:
    """Stands in for yt-dlp: lists per channel and answers per video, remembering what was asked."""

    def __init__(self, lists):
        self.lists, self.listed, self.looked_up = lists, [], []
        self.by_id = {v.id: v for videos in lists.values() if not isinstance(videos, Exception) for v in videos}

    def list(self, url):
        self.listed.append(url)
        answer = self.lists.get(url, [])
        if isinstance(answer, Exception):
            raise answer
        return answer

    def info(self, video_id):
        self.looked_up.append(video_id)
        return self.by_id[video_id]


@pytest.fixture
def seeded(db):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)


def run(db, channel, *, published=None, limits=LIMITS):
    return dt.tick(
        db, cs.ROOT, lister=channel.list, detail=channel.info, limits=limits,
        publish_job=published.append if published is not None else None, now=NOW,
    )  # fmt: skip


def rows(db, sql):
    with db.connect() as conn:
        return conn.execute(sa.text(sql)).mappings().all()


def test_the_best_video_of_a_source_that_is_not_live_becomes_a_download_job_and_an_ended_stream(db, seeded):
    channel = Channel({LEO: [vid(1, views=10), vid(2, views=9000)], JEJE: []})
    published: list = []
    result = run(db, channel, published=published)
    assert result.queued == ["video000002"]  # the most viewed; the backfill for a first run is one
    (job, *_) = rows(
        db,
        "SELECT j.id, j.stage, j.status, j.data, j.spec, s.source_key "
        "FROM jobs j JOIN sources s ON s.id = j.source_id ORDER BY j.id",
    )
    assert (job["stage"], job["status"]) == ("download", "queued") and job["source_key"] == "leokabuki"
    assert (
        job["data"]["video_id"] == "video000002" and job["data"]["start_s"] == 1080 and job["data"]["length_s"] == 3600
    )  # no graph: a little in
    assert job["spec"]["source"]["clip_policy"] == "open"  # a real source: its clips go through the normal review
    streams = rows(db, "SELECT video_id, status, ended_at FROM live_streams ORDER BY video_id")
    assert [(s["video_id"], s["status"]) for s in streams] == [("video000002", "ended")]
    assert all(s["ended_at"] is not None for s in streams)  # so the recorder never touches them
    assert len(published) == 1 and rows(db, "SELECT stream_id::text FROM segments") == []


def test_the_stretch_to_fetch_follows_the_most_replayed_graph(db, seeded):
    heat = tuple((i * 100.0, (i + 1) * 100.0, 1.0 if 40 <= i < 46 else 0.1) for i in range(72))
    channel = Channel({LEO: [vid(1, heat=heat)], JEJE: []})
    run(db, channel)
    (job,) = rows(db, "SELECT data FROM jobs")
    assert 2400 <= job["data"]["start_s"] <= 2600 and job["data"]["length_s"] == 3600  # the busy part is in the middle


def test_a_video_is_never_queued_twice_and_the_first_batch_uses_up_the_turn(db, seeded):
    channel = Channel({LEO: [vid(n) for n in range(1, 6)], JEJE: []})
    assert len(run(db, channel).queued) == 1  # mlbb backfill: one
    assert run(db, channel).queued == []  # today's turn is used
    assert len(rows(db, "SELECT 1 FROM jobs")) == 1


def test_after_the_first_batch_a_source_gets_its_daily_number_once_a_day(db, seeded):
    channel = Channel({LEO: [vid(n) for n in range(1, 8)], JEJE: []})
    run(db, channel)
    with db.begin() as conn:
        conn.execute(sa.text("UPDATE jobs SET created_at = clock_timestamp() - interval '25 hours'"))
    assert len(run(db, channel).queued) == 1  # mlbb: one a day


def test_a_source_that_is_live_right_now_is_left_to_the_recorder(db, seeded):
    with db.begin() as conn:
        source = conn.execute(sa.text("SELECT id FROM sources WHERE source_key = 'leokabuki'")).scalar_one()
        conn.execute(
            sa.text("INSERT INTO live_streams (source_id, video_id, status) VALUES (:s, 'livenow0001', 'live')"),
            {"s": source},
        )
    channel = Channel({LEO: [vid(1)], JEJE: [vid(2)]})
    assert run(db, channel).queued == ["video000002"]
    assert LEO not in channel.listed


@pytest.mark.parametrize("how", ["paused", "blocked"])
def test_a_paused_or_blocked_source_is_never_touched(db, seeded, how):
    with db.begin() as conn:
        source = conn.execute(sa.text("SELECT id FROM sources WHERE source_key = 'leokabuki'")).scalar_one()
        if how == "paused":
            sources.pause(conn, source, updated_by="t")
        else:
            conn.execute(sa.text("UPDATE sources SET clip_policy = 'blocked' WHERE id = :i"), {"i": source})
    channel = Channel({LEO: [vid(1)], JEJE: []})
    assert run(db, channel).queued == [] and LEO not in channel.listed


def test_members_only_and_private_videos_are_never_queued(db, seeded):
    channel = Channel({LEO: [vid(1, avail="subscriber_only"), vid(2, avail="private"), vid(3)], JEJE: []})
    assert run(db, channel).queued == ["video000003"]


def test_a_video_older_than_the_window_or_off_topic_is_skipped_and_the_reason_is_reported(db, seeded):
    channel = Channel({LEO: [vid(1, age=90), vid(2, title="Tanya jawab santai")], JEJE: []})
    result = run(db, channel)
    assert result.queued == [] and any("video000002" in s and "not wanted" in s for s in result.skipped)
    assert any("video000001" in s and "too old" in s for s in result.skipped)


def test_nothing_is_fetched_while_groq_is_more_than_half_used_today(db, seeded):
    with db.begin() as conn:
        ledger.record_cost(
            conn,
            profile="mlbb",
            service="understand",
            provider="groq",
            units=Decimal(15000),
            unit="audio_s",
            usd=Decimal(0),
        )
    channel = Channel({LEO: [vid(1)], JEJE: [vid(2)]})
    result = run(db, channel)
    assert result.queued == [] and channel.listed == [] and any("Groq" in s for s in result.skipped)


def test_nothing_is_fetched_while_too_much_is_already_waiting(db, seeded):
    profile = None
    with db.begin() as conn:
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        profile = profiles[0]
        for _ in range(7):
            jobs.create_job(conn, profile, stage="understand")
    channel = Channel({LEO: [vid(1)], JEJE: [vid(2)]})
    result = run(db, channel)
    assert result.queued == [] and channel.listed == [] and any("waiting" in s for s in result.skipped)


def test_jobs_waiting_for_the_llm_to_come_back_count_as_waiting_so_nothing_new_is_fetched(db, seeded):
    with db.begin() as conn:
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        for _ in range(7):
            job_id = jobs.create_job(conn, profiles[0], stage="select")
            jobs.defer(conn, job_id, 3600, "every LLM provider is out of quota")
    channel = Channel({LEO: [vid(1)], JEJE: [vid(2)]})
    result = run(db, channel)
    assert result.queued == [] and channel.listed == [] and any("waiting" in s for s in result.skipped)


def test_nothing_is_fetched_while_the_llm_quota_is_used_up_even_with_nothing_waiting(db, seeded):
    from common import pauses

    with db.begin() as conn:
        pauses.start(conn, "llm:openai_compatible/gemini-3.5-flash", 3600, "every LLM provider is out of quota")
    channel = Channel({LEO: [vid(1)], JEJE: [vid(2)]})
    result = run(db, channel)
    assert result.queued == [] and channel.listed == [] and any("quota" in s for s in result.skipped)


def test_a_pause_that_has_ended_does_not_stop_discover(db, seeded):
    from common import pauses

    with db.begin() as conn:
        pauses.start(conn, "llm:x/y", 600, "out of quota")
        conn.execute(sa.text("UPDATE pauses SET until = clock_timestamp() - interval '1 minute'"))
    assert run(db, Channel({LEO: [vid(1)], JEJE: []})).queued == ["video000001"]


def test_a_channel_that_cannot_be_read_is_reported_and_the_other_source_still_goes(db, seeded):
    channel = Channel({LEO: DiscoverError("HTTP 429"), JEJE: [vid(2)]})
    result = run(db, channel)
    assert result.queued == ["video000002"] and any("429" in p for p in result.problems)


def test_a_source_with_nothing_to_take_does_not_cost_a_lookup_per_video(db, seeded):
    channel = Channel({LEO: [vid(1)], JEJE: []})
    run(db, channel)
    channel.looked_up.clear()
    run(db, channel)  # the turn is used: no listing's videos are looked up again
    assert channel.looked_up == []


def test_a_failing_queue_leaves_the_job_for_the_reconciler(db, seeded):
    def down(job_id):
        raise ConnectionError("redis is down")

    channel = Channel({LEO: [vid(1)], JEJE: []})
    result = dt.tick(db, cs.ROOT, lister=channel.list, detail=channel.info, limits=LIMITS, publish_job=down, now=NOW)
    assert result.queued == ["video000001"] and rows(db, "SELECT status FROM jobs") == [{"status": "queued"}]


def test_nothing_is_fetched_while_intake_is_paused(db, seeded):
    from common import pauses

    with db.begin() as conn:
        pauses.hold(conn, "switch:intake", "paused from Telegram")
    channel = Channel({LEO: [vid(1)], JEJE: [vid(2)]})
    result = run(db, channel)
    assert result.queued == [] and channel.listed == [] and any("paused" in s for s in result.skipped)


def test_only_the_ended_switch_stops_discovery_not_the_live_one(db, seeded):
    from common import pauses

    with db.begin() as conn:
        pauses.hold(conn, "switch:live", "paused from Telegram")  # live recordings: nothing to do with past streams
    channel = Channel({LEO: [vid(1)], JEJE: [vid(2)]})
    assert run(db, channel).queued != []
    with db.begin() as conn:
        pauses.release(conn, "switch:live")
        pauses.hold(conn, "switch:ended", "paused from Telegram")
    other = Channel({LEO: [vid(3)]})
    result = run(db, other)
    assert result.queued == [] and other.listed == [] and any("ended" in s for s in result.skipped)
