"""Running a recording for a live stream row: lease, heartbeat, and what happens when it ends (FR-15, NFR-11)."""

from __future__ import annotations

import threading
import time

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import leases, sources
from common.media import LocalStore
from services.live_watcher import detect
from services.recorder import record as rec
from services.recorder import run

pytestmark = pytest.mark.integration


@pytest.fixture
def stream(db):
    with db.begin() as conn:
        return conn.execute(
            sa.text("INSERT INTO live_streams (video_id) VALUES ('vid00001') RETURNING id")
        ).scalar_one()


def row(db, stream_id):
    with db.connect() as conn:
        return conn.execute(sa.text("SELECT * FROM live_streams WHERE id = :i"), {"i": stream_id}).mappings().one()


class Recording:
    """A fake `record`: remembers its arguments, optionally reports segments, then returns or raises."""

    def __init__(self, result="ended", error=None, wait=0.0, segments=0):
        self.result, self.error, self.wait, self.segments, self.calls = result, error, wait, segments, []

    def __call__(
        self, url, store, stream_id, *, segment_s, on_segment, quality, stop, start_index=0, start_s=0.0,
        min_quality=None,
    ):  # fmt: skip
        self.calls.append((url, stream_id, segment_s, quality))
        self.min_quality = min_quality
        self.starts = (start_index, start_s)
        for i in range(self.segments):
            on_segment(rec.Segment(i, f"segments/{stream_id}/{i:05d}.ts", i * 4.0, i * 4.0 + 4))
        if self.wait:
            stop.wait(self.wait)
        if self.error:
            raise self.error
        return "stopped" if stop.is_set() else self.result


def go(
    db,
    stream,
    tmp_path,
    recording,
    *,
    stop=None,
    on_segment=lambda s: None,
    still_live=lambda v: False,
    profile=None,
    publish_job=None,
):
    return run.run_stream(
        db, LocalStore(tmp_path), stream, "w1", segment_s=600, quality="720p", ttl_s=60, heartbeat_s=0.1,
        on_segment=on_segment, record=recording, stop=stop, still_live=still_live,
        profile=profile, publish_job=publish_job,
    )  # fmt: skip


def test_a_stream_that_ends_is_marked_ended_and_released(db, stream, tmp_path):
    recording = Recording("ended")
    assert go(db, stream, tmp_path, recording) == "ended"
    after = row(db, stream)
    assert after["status"] == "ended" and after["ended_at"] is not None and after["lease_worker"] is None
    assert recording.calls == [("https://www.youtube.com/watch?v=vid00001", str(stream), 600, "720p")]


def test_the_minimum_quality_is_handed_to_the_recording(db, stream, tmp_path):
    recording = Recording("ended")
    run.run_stream(
        db, LocalStore(tmp_path), stream, "w1", segment_s=600, quality="1080p", min_quality="720p", ttl_s=60,
        heartbeat_s=0.1, on_segment=lambda s: None, record=recording, still_live=lambda v: False,
    )  # fmt: skip
    assert recording.min_quality == "720p"


def test_a_stream_that_only_offers_a_quality_below_the_minimum_ends_without_a_recording(db, stream, tmp_path):
    """Streamlink finds nothing to play, the recording fails, and the stream is over: it is settled as ended with no
    segments, so the hours are left to the download path instead of being recorded in 360p."""
    refused = Recording(error=rec.RecorderError("streamlink exited with 1: the specified stream(s) 'best' not found"))
    with pytest.raises(rec.RecorderError):
        go(db, stream, tmp_path, refused, still_live=lambda v: False)
    after = row(db, stream)
    assert after["status"] == "ended" and after["lease_worker"] is None


def test_the_same_refusal_while_the_stream_is_still_live_is_tried_again_later(db, stream, tmp_path):
    refused = Recording(error=rec.RecorderError("streamlink exited with 1: the specified stream(s) 'best' not found"))
    with pytest.raises(rec.RecorderError):
        go(db, stream, tmp_path, refused, still_live=lambda v: True)
    assert row(db, stream)["status"] == "live"  # a live stream's better qualities often appear a little later


def test_a_stop_releases_the_lease_but_leaves_the_stream_live_for_another_worker(db, stream, tmp_path):
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    assert go(db, stream, tmp_path, Recording(wait=10), stop=stop) == "stopped"
    after = row(db, stream)
    assert after["status"] == "live" and after["lease_worker"] is None


def test_a_failed_recording_keeps_the_stream_live_and_releases_the_lease(db, stream, tmp_path):
    with pytest.raises(rec.RecorderError, match="boom"):
        go(db, stream, tmp_path, Recording(error=rec.RecorderError("boom")), still_live=lambda v: True)
    after = row(db, stream)
    assert after["status"] == "live" and after["lease_worker"] is None


def test_a_stream_someone_else_is_recording_is_left_alone(db, stream, tmp_path):
    with db.begin() as conn:
        assert leases.acquire(conn, stream, "other", 60)
    recording = Recording()
    assert go(db, stream, tmp_path, recording) == "busy"
    assert recording.calls == [] and row(db, stream)["lease_worker"] == "other"


def test_the_lease_is_refreshed_while_recording(db, stream, tmp_path):
    seen = []

    def watch():
        time.sleep(0.25)
        seen.append(row(db, stream)["lease_heartbeat_at"])
        time.sleep(0.35)
        seen.append(row(db, stream)["lease_heartbeat_at"])

    thread = threading.Thread(target=watch)
    thread.start()
    go(db, stream, tmp_path, Recording(wait=0.8))
    thread.join()
    assert seen[0] is not None and seen[1] > seen[0]


def test_losing_the_lease_stops_the_recording(db, stream, tmp_path):
    def steal():
        time.sleep(0.2)
        with db.begin() as conn:
            conn.execute(sa.text("UPDATE live_streams SET lease_worker = 'other'"))

    threading.Thread(target=steal).start()
    started = time.monotonic()
    assert go(db, stream, tmp_path, Recording(wait=10)) == "lost"
    assert time.monotonic() - started < 3
    assert row(db, stream)["lease_worker"] == "other"  # not released: it is no longer ours


def test_each_finished_segment_is_passed_on(db, stream, tmp_path):
    seen: list[rec.Segment] = []
    go(db, stream, tmp_path, Recording(segments=3), on_segment=seen.append)
    assert [s.index for s in seen] == [0, 1, 2]


def stored(db, stream_id):
    with db.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT idx, key, start_s, end_s FROM segments WHERE stream_id = :i ORDER BY idx"), {"i": stream_id}
        )
        return [tuple(r) for r in rows]


def test_every_finished_segment_is_stored_once(db, stream, tmp_path):
    go(db, stream, tmp_path, Recording(segments=3))
    assert [r[0] for r in stored(db, stream)] == [0, 1, 2]
    assert stored(db, stream)[1] == (1, f"segments/{stream}/00001.ts", 4.0, 8.0)


def test_a_segment_reported_twice_is_stored_once(db, stream, tmp_path):
    def twice(url, store, stream_id, *, on_segment, **kw):
        segment = rec.Segment(0, f"segments/{stream_id}/00000.ts", 0.0, 4.0)
        on_segment(segment)
        on_segment(segment)
        return "ended"

    go(db, stream, tmp_path, twice)
    assert len(stored(db, stream)) == 1


def test_a_restart_carries_on_after_the_last_stored_segment(db, stream, tmp_path):
    go(db, stream, tmp_path, Recording(segments=3))  # leaves segments 0..2, the last one ending at 12 s
    again = Recording(segments=0)
    go(db, stream, tmp_path, again)
    assert again.starts == (3, 12.0)


def test_a_first_recording_starts_from_zero(db, stream, tmp_path):
    first = Recording()
    go(db, stream, tmp_path, first)
    assert first.starts == (0, 0.0)


def test_an_ended_recording_of_a_stream_that_is_still_live_is_a_drop_not_the_end(db, stream, tmp_path):
    assert go(db, stream, tmp_path, Recording("ended"), still_live=lambda v: True) == "dropped"
    after = row(db, stream)
    assert after["status"] == "live" and after["ended_at"] is None and after["lease_worker"] is None


def test_a_stream_that_could_not_be_checked_is_treated_as_still_live(db, stream, tmp_path):
    def broken(video_id):
        raise detect.DetectError("HTTP Error 429")

    assert go(db, stream, tmp_path, Recording("ended"), still_live=broken) == "dropped"
    assert row(db, stream)["status"] == "live"


def test_a_recording_that_fails_on_a_stream_that_is_over_ends_it(db, stream, tmp_path):
    with pytest.raises(rec.RecorderError):
        go(db, stream, tmp_path, Recording(error=rec.RecorderError("no playable streams")), still_live=lambda v: False)
    assert row(db, stream)["status"] == "ended"


# ---------- each new segment becomes a job at the understand stage ------------------------------------------------
@pytest.fixture
def sourced(db):
    """A live stream that belongs to the seeded leokabuki source, and that source's profile."""
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        source_id = conn.execute(sa.text("SELECT id FROM sources WHERE source_key = 'leokabuki'")).scalar_one()
        stream_id = conn.execute(
            sa.text("INSERT INTO live_streams (video_id, source_id) VALUES ('vid00001', :s) RETURNING id"),
            {"s": source_id},
        ).scalar_one()
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
    return stream_id, source_id, next(p for p in profiles if p.source.id == "leokabuki")


def job_rows(db):
    with db.connect() as conn:
        return conn.execute(sa.text("SELECT * FROM jobs ORDER BY created_at, id")).mappings().all()


def test_each_new_segment_becomes_a_queued_understand_job_and_is_published(db, sourced, tmp_path):
    stream, source_id, profile = sourced
    published: list = []
    go(db, stream, tmp_path, Recording(segments=3), profile=profile, publish_job=published.append)
    jobs_ = job_rows(db)
    assert len(jobs_) == 3 and [j["id"] for j in jobs_] == published
    first = jobs_[1]
    assert first["stage"] == "understand" and first["status"] == "queued" and first["source_id"] == source_id
    assert first["media_key"] == f"segments/{stream}/00001.ts"
    assert first["spec"]["name"] == profile.name and first["spec"]["source"]["id"] == "leokabuki"
    assert first["data"] == {
        "live": True, "stream_id": str(stream), "video_id": "vid00001", "segment": 1, "start_s": 4.0, "end_s": 8.0,
    }  # fmt: skip


def test_a_segment_reported_twice_makes_one_job(db, sourced, tmp_path):
    stream, _, profile = sourced

    def twice(url, store, stream_id, *, on_segment, **kw):
        segment = rec.Segment(0, f"segments/{stream_id}/00000.ts", 0.0, 4.0)
        on_segment(segment)
        on_segment(segment)
        return "ended"

    published: list = []
    go(db, stream, tmp_path, twice, profile=profile, publish_job=published.append)
    assert len(job_rows(db)) == 1 and len(published) == 1


def test_without_a_profile_segments_are_stored_but_no_job_is_made(db, stream, tmp_path):
    go(db, stream, tmp_path, Recording(segments=2))
    assert len(stored(db, stream)) == 2 and job_rows(db) == []


def test_a_queue_that_is_down_does_not_stop_the_recording_and_the_job_stays_queued(db, sourced, tmp_path):
    stream, _, profile = sourced

    def down(job_id):
        raise ConnectionError("redis is down")

    assert go(db, stream, tmp_path, Recording(segments=2), profile=profile, publish_job=down) == "ended"
    assert [j["status"] for j in job_rows(db)] == ["queued", "queued"]  # the reconciler publishes them later
    assert len(stored(db, stream)) == 2
