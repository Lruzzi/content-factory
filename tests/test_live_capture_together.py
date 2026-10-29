"""Live-watcher and recorder working together: a stream the watcher finds ends up recorded as segments on disk."""

from __future__ import annotations

import json
import threading

import pytest
import sqlalchemy as sa
from test_recorder import LiveSource, needs_tools

import config_schema as cs
from common import pipeline, sources
from common.media import LocalStore
from services.live_watcher import detect, watcher
from services.recorder import record as rec
from services.recorder.service import RecorderService

pytestmark = pytest.mark.integration

CONFIG = cs.ROOT
LEO = "https://www.youtube.com/@leokabuki"


def found(video_id):
    return detect.Detection("live", video_id, "u", "t", "")


def service(
    db, tmp_path, *, max_concurrent=3, on_segment=None, url_for=None, record=None, queue=None, alert=None, live=False,
    clock=None, max_failures=None,
):  # fmt: skip
    extra = {k: v for k, v in {"url_for": url_for, "record": record}.items() if v is not None}
    if alert is not None:
        extra["alert"] = alert
    if clock is not None:
        extra["clock"] = clock
    if max_failures is not None:
        extra["max_failures"] = max_failures
    if queue is not None:
        extra["redis_client"], extra["prefix"] = queue
    extra["still_live"] = lambda video_id: live  # the feeds here are fake: nothing to look up on YouTube
    return RecorderService(
        db, LocalStore(tmp_path / "media"), CONFIG, "w1", max_concurrent=max_concurrent, ttl_s=60, heartbeat_s=0.2,
        on_segment=on_segment or (lambda stream_id, segment: None), **extra,
    )  # fmt: skip


def open_streams(db, *video_ids):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, CONFIG)
        for video_id in video_ids:
            watcher.tick(conn, CONFIG, lambda url, v=video_id: found(v))  # type: ignore[misc]
            conn.execute(sa.text("UPDATE live_streams SET status = 'live' WHERE video_id = :v"), {"v": video_id})


@needs_tools
def test_a_stream_the_watcher_finds_is_recorded_to_disk_and_marked_ended(db, tmp_path):
    feed = LiveSource(tmp_path / "feed", 12)
    (tmp_path / "feed").mkdir()
    feed.start()
    seen: list[tuple[str, rec.Segment]] = []
    try:
        open_streams(db, "vid00001")
        recorder = service(db, tmp_path, url_for=lambda video_id: feed.url, on_segment=lambda i, s: seen.append((i, s)))
        assert len(recorder.poll()) == 1
        recorder.join(60)
    finally:
        feed.stop()
    with db.connect() as conn:
        stream = conn.execute(sa.text("SELECT * FROM live_streams")).mappings().one()
    assert stream["status"] == "ended" and stream["lease_worker"] is None
    assert seen and all(i == str(stream["id"]) for i, _ in seen)
    assert all(LocalStore(tmp_path / "media").exists(s.key) for _, s in seen)
    assert recorder.poll() == []  # an ended stream is not started again


def test_no_more_recordings_than_allowed_run_at_once(db, tmp_path):
    release = threading.Event()

    def slow(url, store, stream_id, *, segment_s, on_segment, quality, stop, **kw):
        release.wait(10)
        return "ended"

    open_streams(db, "vid00001", "vid00002")
    recorder = service(db, tmp_path, max_concurrent=1, record=slow)
    assert len(recorder.poll()) == 1
    assert recorder.poll() == []  # full, and the running one is not started twice
    release.set()
    recorder.join(10)
    assert len(recorder.poll()) == 1  # room again: the second stream


def test_a_stream_of_a_paused_source_is_not_recorded(db, tmp_path):
    open_streams(db, "vid00001")
    with db.begin() as conn:
        sources.pause(  # the stream belongs to whichever of the two seeded sources the fake watcher asked first
            conn, conn.execute(sa.text("SELECT source_id FROM live_streams")).scalar_one(), updated_by="t"
        )
    assert service(db, tmp_path).poll() == []


def test_finished_segments_reach_the_understand_queue(db, tmp_path, redis_client, stream_prefix):
    def two_segments(url, store, stream_id, *, on_segment, **kw):
        for i in range(2):
            on_segment(rec.Segment(i, f"segments/{stream_id}/{i:05d}.ts", i * 4.0, i * 4.0 + 4))
        return "ended"

    open_streams(db, "vid00001")
    recorder = service(db, tmp_path, record=two_segments, queue=(redis_client, stream_prefix))
    recorder.poll()
    recorder.join(10)
    with db.connect() as conn:
        ids = [
            str(i)
            for i in conn.execute(
                sa.text("SELECT id FROM jobs WHERE stage = 'understand' ORDER BY created_at, id")
            ).scalars()
        ]
    queued = redis_client.xrange(pipeline.stream_for("understand", stream_prefix))
    assert len(ids) == 2
    assert [json.loads(fields["data"])["job_id"] for _, fields in queued] == ids


def test_a_recording_that_fails_raises_an_alert_and_the_stream_stays_live_to_be_retried(db, tmp_path):
    def broken(url, store, stream_id, **kw):
        raise rec.RecorderError("streamlink exited with 1: no playable streams")

    alerts: list[str] = []
    open_streams(db, "vid00001")
    recorder = service(db, tmp_path, record=broken, alert=alerts.append, live=True)
    recorder.poll()
    recorder.join(10)
    assert len(alerts) == 1 and "no playable streams" in alerts[0]
    with db.connect() as conn:
        assert conn.execute(sa.text("SELECT status FROM live_streams")).scalar_one() == "live"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def failing_service(db, tmp_path, calls, **kw):
    def broken(url, store, stream_id, **kwargs):
        calls.append(1)
        raise rec.RecorderError("streamlink exited with 1: no playable streams")

    return service(db, tmp_path, record=broken, live=True, **kw)


def attempt(recorder):
    recorder.poll()
    recorder.join(10)


def test_a_stream_that_keeps_failing_is_retried_with_growing_pauses_not_every_ten_seconds(db, tmp_path):
    calls: list[int] = []
    clock = Clock()
    open_streams(db, "vid00001")
    recorder = failing_service(db, tmp_path, calls, clock=clock)
    attempt(recorder)
    assert len(calls) == 1
    attempt(recorder)
    assert len(calls) == 1  # still in the first pause
    clock.now += 11
    attempt(recorder)
    assert len(calls) == 2
    clock.now += 15  # the second pause is twice as long (20 s)
    attempt(recorder)
    assert len(calls) == 2
    clock.now += 6
    attempt(recorder)
    assert len(calls) == 3


def test_the_pause_never_grows_beyond_five_minutes(db, tmp_path):
    calls: list[int] = []
    clock = Clock()
    open_streams(db, "vid00001")
    recorder = failing_service(db, tmp_path, calls, clock=clock, max_failures=50)
    for _ in range(12):
        attempt(recorder)
        clock.now += 301
    assert len(calls) == 12  # with a 300 s ceiling every 301 s step is long enough


def test_after_too_many_failures_the_recording_is_given_up_with_an_alert_and_never_started_again(db, tmp_path):
    calls: list[int] = []
    alerts: list[str] = []
    clock = Clock()
    open_streams(db, "vid00001")
    recorder = failing_service(db, tmp_path, calls, clock=clock, max_failures=3, alert=alerts.append)
    for _ in range(5):
        attempt(recorder)
        clock.now += 400
    assert len(calls) == 3
    assert any("gave up" in a for a in alerts)
    with db.connect() as conn:
        assert conn.execute(sa.text("SELECT status FROM live_streams")).scalar_one() == "ended"


def test_a_recording_that_produced_a_segment_starts_its_failure_count_again(db, tmp_path):
    calls: list[int] = []
    clock = Clock()

    def one_segment_then_fail(url, store, stream_id, *, on_segment, **kwargs):
        calls.append(1)
        on_segment(rec.Segment(len(calls), f"segments/{stream_id}/{len(calls):05d}.ts", 0.0, 4.0))
        raise rec.RecorderError("connection dropped")

    open_streams(db, "vid00001")
    recorder = service(db, tmp_path, record=one_segment_then_fail, live=True, clock=clock, max_failures=3)
    for _ in range(6):
        attempt(recorder)
        clock.now += 400
    assert len(calls) == 6  # it was never given up: each attempt made progress


def test_no_recording_is_started_while_intake_is_paused(db, tmp_path):
    from common import pauses

    open_streams(db, "vid00001")
    recorder = service(db, tmp_path)
    with db.begin() as conn:
        pauses.hold(conn, "switch:intake", "paused from Telegram")
    assert recorder.poll() == []
    with db.begin() as conn:
        pauses.release(conn, "switch:intake")
    assert len(recorder.poll()) == 1
    recorder.join(30)


def test_a_recording_is_stopped_by_the_live_switch_and_not_by_the_ended_one(db, tmp_path):
    from common import pauses

    open_streams(db, "vid00001")
    recorder = service(db, tmp_path)
    with db.begin() as conn:
        pauses.hold(conn, "switch:live", "paused from Telegram")
    assert recorder.poll() == []
    with db.begin() as conn:
        pauses.release(conn, "switch:live")
        pauses.hold(conn, "switch:ended", "paused from Telegram")
    assert len(recorder.poll()) == 1
    recorder.join(30)
