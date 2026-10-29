"""Deleting what is no longer needed so the disk does not fill: source recordings once nothing waits for them, and clips
once they were rejected, expired or published everywhere (FR-15: bounded disk use)."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import jobs, sources
from common.media import LocalStore
from services.scaler import retention

pytestmark = pytest.mark.integration

POLICY = retention.Policy(published_keep_h=1, failed_keep_days=3, approved_max_hold_days=3)
STREAM = "00000000-0000-0000-0000-0000000000aa"


class World:
    def __init__(self, db, tmp_path):
        self.db, self.store, self.alerts = db, LocalStore(tmp_path / "media"), []
        with db.begin() as conn:
            sources.seed_from_yaml(conn, cs.ROOT)
            profiles, _ = sources.load_profiles(conn, cs.ROOT)
            self.profile = next(p for p in profiles if p.source.id == "leokabuki")
            conn.execute(sa.text("INSERT INTO live_streams (id, video_id) VALUES (:i, 'vid00001')"), {"i": STREAM})

    def file(self, key: str, size: int = 100) -> None:
        path = self.store.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)

    def segment(self, index: int, *, age_h: float, jobs_in: list[dict[str, Any]] | None = None, with_file=True):
        key = f"segments/{STREAM}/{index:05d}.ts"
        if with_file:
            self.file(key)
        with self.db.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO segments (stream_id, idx, key, start_s, end_s, created_at) VALUES "
                    "(:s, :i, :k, 0, 10, clock_timestamp() - make_interval(secs => :h))"
                ),
                {"s": STREAM, "i": index, "k": key, "h": age_h * 3600},
            )
            for spec in jobs_in or []:
                self._job(conn, key, **spec)
        return key

    def _job(self, conn, key, *, stage, status="queued", render_source=None, buffer_h=None, hold_h=None):
        profile = self.profile.model_copy(deep=True)
        if buffer_h:
            profile.live.max_buffer_h = buffer_h
        if hold_h:
            profile.live.max_hold_h = hold_h
        data = {"render": {"source_key": render_source}} if render_source else {}
        job_id = jobs.create_job(conn, profile, stage=stage, media_key=key, data=data)
        conn.execute(sa.text("UPDATE jobs SET status = :s WHERE id = :i"), {"s": status, "i": job_id})
        return job_id

    def clip(self, name, *, stage, status="queued", review=None, age_h=0.0, posts=(), key=None):
        key = key or f"clips/{name}.mp4"
        if not key.startswith(".."):
            self.file(key)
        data: dict[str, Any] = {"render": {"output": {"key": key}}}
        if review:
            data["review"] = {"state": review}
        with self.db.begin() as conn:
            job_id = jobs.create_job(conn, self.profile, stage=stage, media_key=key, data=data)
            conn.execute(
                sa.text(
                    "UPDATE jobs SET status = :s, "
                    "updated_at = clock_timestamp() - make_interval(secs => :h) WHERE id = :i"
                ),
                {"s": status, "h": age_h * 3600, "i": job_id},
            )
            for account, state in posts:
                conn.execute(
                    sa.text(
                        "INSERT INTO posts (job_id, account_id, platform, idempotency_key, state) "
                        "VALUES (:j, :a, 'shorts', :k, :s)"
                    ),
                    {"j": job_id, "a": account, "k": f"{job_id}:{account}", "s": state},
                )
        return key

    def sweep(self):
        return retention.sweep(self.db, self.store, POLICY, alert=self.alerts.append)

    def exists(self, key):
        return self.store.path(key).is_file()

    def deleted_at(self, key):
        with self.db.connect() as conn:
            return conn.execute(sa.text("SELECT deleted_at FROM segments WHERE key = :k"), {"k": key}).scalar_one()


@pytest.fixture
def w(db, tmp_path):
    return World(db, tmp_path)


# ======================= source recordings =======================
def test_a_recent_segment_is_kept_even_when_nothing_needs_it(w):
    key = w.segment(0, age_h=1)
    w.sweep()
    assert w.exists(key) and w.deleted_at(key) is None


def test_an_old_segment_nothing_needs_is_deleted_and_marked(w):
    key = w.segment(0, age_h=4, jobs_in=[{"stage": "select", "status": "done"}])
    result = w.sweep()
    assert not w.exists(key) and w.deleted_at(key) is not None and result.segments == 1


def test_an_old_segment_waiting_for_transcription_is_kept(w):
    key = w.segment(0, age_h=5, jobs_in=[{"stage": "understand", "status": "queued"}])
    w.sweep()
    assert w.exists(key)


def test_an_old_segment_is_kept_while_a_clip_still_has_to_be_rendered_from_it_and_released_after(w):
    key = f"segments/{STREAM}/00000.ts"
    w.segment(
        0,
        age_h=5,
        jobs_in=[{"stage": "select", "status": "done"}, {"stage": "render", "status": "running", "render_source": key}],
    )
    w.sweep()
    assert w.exists(key)
    with w.db.begin() as conn:
        conn.execute(sa.text("UPDATE jobs SET stage = 'package', status = 'queued' WHERE stage = 'render'"))
    w.sweep()
    assert not w.exists(key)


def test_a_recording_is_kept_while_a_recap_still_has_to_be_rendered_from_pieces_of_it(w):
    key = f"segments/{STREAM}/00000.ts"
    other = f"segments/{STREAM}/00001.ts"
    w.segment(0, age_h=5, jobs_in=[{"stage": "select", "status": "done"}])
    w.segment(1, age_h=5, jobs_in=[{"stage": "select", "status": "done"}])
    with w.db.begin() as conn:
        pieces = [
            {"source_key": other, "start_s": 1.0, "end_s": 5.0},
            {"source_key": key, "start_s": 2.0, "end_s": 6.0},
        ]
        job_id = jobs.create_job(
            conn, w.profile, stage="render", media_key=other, kind="recap", data={"render": {"pieces": pieces}}
        )
        conn.execute(sa.text("UPDATE jobs SET status = 'running' WHERE id = :i"), {"i": job_id})
    w.sweep()
    assert w.exists(key) and w.exists(other)  # both files hold pieces of the recap
    with w.db.begin() as conn:
        conn.execute(sa.text("UPDATE jobs SET stage = 'package', status = 'queued' WHERE kind = 'recap'"))
    w.sweep()
    assert not w.exists(key) and not w.exists(other)


def test_a_clip_that_failed_for_good_does_not_hold_the_recording(w):
    key = f"segments/{STREAM}/00000.ts"
    w.segment(0, age_h=5, jobs_in=[{"stage": "render", "status": "dead", "render_source": key}])
    w.sweep()
    assert not w.exists(key)


def test_at_the_hold_ceiling_the_recording_goes_even_if_a_job_is_stuck_and_you_are_told_which(w):
    key = w.segment(0, age_h=13, jobs_in=[{"stage": "understand", "status": "failed"}])
    w.sweep()
    assert not w.exists(key) and len(w.alerts) == 1 and "00000.ts" in w.alerts[0]


def test_each_recording_follows_the_buffer_of_its_own_profile(w):
    short = w.segment(0, age_h=2, jobs_in=[{"stage": "select", "status": "done", "buffer_h": 1}])
    normal = w.segment(1, age_h=2, jobs_in=[{"stage": "select", "status": "done"}])
    w.sweep()
    assert not w.exists(short) and w.exists(normal)


def test_a_segment_nobody_made_a_job_for_uses_the_defaults(w):
    young, old = w.segment(0, age_h=2), w.segment(1, age_h=4)
    w.sweep()
    assert w.exists(young) and not w.exists(old)


def test_a_segment_already_deleted_is_not_looked_at_again_and_a_missing_file_is_no_error(w):
    key = w.segment(0, age_h=4, with_file=False)
    assert w.sweep().segments == 1 and w.deleted_at(key) is not None
    assert w.sweep().segments == 0


# ======================= clips =======================
def test_a_rejected_or_expired_clip_is_deleted_at_once(w):
    rejected = w.clip("a", stage="review", status="done", review="rejected")
    expired = w.clip("b", stage="review", status="done", review="expired")
    result = w.sweep()
    assert not w.exists(rejected) and not w.exists(expired) and result.clips == 2


def test_a_clip_waiting_for_your_decision_is_kept(w):
    key = w.clip("a", stage="review", status="waiting", review="pending", age_h=20)
    w.sweep()
    assert w.exists(key)


def test_an_approved_clip_is_kept_while_it_is_being_published(w):
    key = w.clip("a", stage="publish", status="queued", review="approved", age_h=2, posts=[("yt_test", "posted")])
    w.sweep()
    assert w.exists(key)


def test_a_clip_published_everywhere_is_deleted_after_the_short_grace_period(w):
    fresh = w.clip("a", stage="publish", status="done", review="approved", age_h=0.2, posts=[("yt_test", "posted")])
    stale = w.clip("b", stage="publish", status="done", review="approved", age_h=2, posts=[("yt_test", "posted")])
    w.sweep()
    assert w.exists(fresh) and not w.exists(stale)


def test_a_clip_with_a_failed_post_is_kept_for_days_so_it_can_be_recovered(w):
    soon = w.clip(
        "a",
        stage="publish",
        status="done",
        review="approved",
        age_h=30,
        posts=[("yt_test", "posted"), ("fb_test", "failed")],
    )
    late = w.clip("b", stage="publish", status="done", review="approved", age_h=80, posts=[("fb_test", "failed")])
    w.sweep()
    assert w.exists(soon) and not w.exists(late)


def test_an_approved_clip_that_never_gets_published_is_deleted_at_the_ceiling_with_an_alert(w):
    key = w.clip("a", stage="publish", status="queued", review="approved", age_h=80)
    w.sweep()
    assert not w.exists(key) and len(w.alerts) == 1 and "not published" in w.alerts[0]


def test_a_clip_that_skips_review_is_treated_like_an_approved_one(w):
    key = w.clip("a", stage="publish", status="done", review="auto", age_h=2, posts=[("yt_test", "posted")])
    w.sweep()
    assert not w.exists(key)


def test_a_path_that_points_outside_the_media_folders_is_never_deleted(w, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("precious")
    w.clip("a", stage="review", status="done", review="rejected", key="../outside.txt")
    w.clip("b", stage="review", status="done", review="rejected", key="other/thing.mp4")
    w.sweep()
    assert outside.read_text() == "precious" and w.exists("other/thing.mp4")


def test_a_second_sweep_finds_nothing_more_to_delete(w):
    w.clip("a", stage="review", status="done", review="rejected")
    assert w.sweep().clips == 1 and w.sweep().clips == 0


def test_the_sweep_reports_the_space_it_freed(w):
    w.clip("big", stage="review", status="done", review="rejected")
    w.file("clips/big.mp4", size=5000)  # after the job exists: the helper above writes a small placeholder
    assert w.sweep().bytes == 5000


def test_the_loop_reports_a_failing_sweep_and_keeps_going(w, monkeypatch):
    import threading
    import time

    calls: list[int] = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk is read-only")
        return retention.Swept()

    monkeypatch.setattr(retention, "sweep", flaky)
    stop = threading.Event()
    thread = threading.Thread(
        target=retention.run,
        args=(w.db, w.store, POLICY),
        kwargs={"every_s": 0.05, "stop": stop, "alert": w.alerts.append},
    )
    thread.start()
    deadline = time.monotonic() + 5
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    stop.set()
    thread.join(5)
    assert len(calls) >= 3 and len(w.alerts) == 1 and "disk is read-only" in w.alerts[0]
