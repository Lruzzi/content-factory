"""Posting an approved clip to the YouTube accounts of its profile: rules, idempotency, resuming, and alerts (FR-9)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import yaml
from fake_google_server import fake_google

import config_schema as cs
from common import jobs, sources
from common.media import LocalStore
from services.publisher import accounts as acc
from services.publisher import google_oauth as g
from services.publisher import posts
from services.publisher.shorts import ShortsPublisher
from services.publisher.youtube import YouTube

pytestmark = pytest.mark.integration

CHUNK = 256 * 1024
KEY = "clips/real.mp4"
ALL_TARGETS = ["yt_test", "tt_test", "ig_test", "fb_test"]  # the mlbb profile's test_targets


def accounts_file(tmp_path: Path, **yt: Any) -> Path:
    defaults = {"daily_cap": 3, "min_spacing_min": 0, "post_windows": [], "visibility": "public"}
    rows = [
        {"id": "yt_test", "platform": "shorts", "test": True, "credentials": {"token_file": "tokens/yt_test.json"}}
        | yt,
        {"id": "tt_test", "platform": "tiktok", "test": True, "credentials": {}},
        {"id": "ig_test", "platform": "reels", "test": True, "credentials": {}},
        {"id": "fb_test", "platform": "facebook", "test": True, "credentials": {}},
    ]
    path = tmp_path / "accounts.yaml"
    path.write_text(yaml.safe_dump({"timezone": "Asia/Jakarta", "defaults": defaults, "accounts": rows}))
    return path


# ---------- the accounts file ---------------------------------------------------------------------
def test_each_account_gets_the_defaults_it_does_not_override(tmp_path):
    loaded = acc.load(accounts_file(tmp_path, daily_cap=6, visibility="private"))
    rules = loaded.by_id["yt_test"]
    assert (rules.daily_cap, rules.visibility, rules.min_spacing_min, rules.post_windows) == (6, "private", 0, [])
    assert loaded.by_id["tt_test"].daily_cap == 3 and str(loaded.timezone) == "Asia/Jakarta"


def test_the_real_accounts_file_loads():
    loaded = acc.load(cs.ROOT / "accounts.yaml")
    assert loaded.by_id["yt_test"].platform == "shorts" and loaded.by_id["yt_test"].daily_cap == 6


# ---------- publishing ----------------------------------------------------------------------------
class World:
    def __init__(self, db, tmp_path, fake, url):
        self.db, self.tmp, self.fake, self.url = db, tmp_path, fake, url
        self.store = LocalStore(tmp_path / "media")
        path = self.store.path(KEY)
        path.parent.mkdir(parents=True)
        path.write_bytes(bytes(range(256)) * 3000)  # 750 KiB: three chunks
        self.alerts: list[str] = []
        self.clock: datetime | None = None
        self.token_error: Exception | None = None
        self.token_calls = 0

    def publisher(self, path: Path | None = None, **kw: Any) -> ShortsPublisher:
        def token() -> str:
            self.token_calls += 1
            if self.token_error:
                raise self.token_error
            return "tok"

        def uploader(rules: acc.AccountRules) -> YouTube:
            return YouTube(token, base_url=self.url, chunk_bytes=CHUNK, retry_wait_s=0, max_attempts=2)

        return ShortsPublisher(
            self.db, self.store, acc.load(path or accounts_file(self.tmp)), uploader, alert=self.alerts.append,
            now=self._clock_now if self.clock else None, **kw,
        )  # fmt: skip

    def _clock_now(self) -> datetime:
        assert self.clock is not None
        return self.clock

    def job(self, *, edit=None, targets=None) -> jobs.Job:
        with self.db.begin() as conn:
            sources.seed_from_yaml(conn, cs.ROOT)
            profiles, _ = sources.load_profiles(conn, cs.ROOT)
            profile = next(p for p in profiles if p.source.id == "leokabuki")
            if targets is not None:
                profile.test_targets = [cs.Target(account=t) for t in targets]
            if edit:
                edit(profile)
            source_id = conn.execute(sa.text("SELECT id FROM sources WHERE source_key = 'leokabuki'")).scalar_one()
            data = {
                "render": {"output": {"key": KEY, "duration_s": 26.7}},
                "package": {"shorts": {"title": "Judul klip", "text": "Isi klip\n\n#mlbb", "hashtags": ["#mlbb"]}},
            }
            job_id = jobs.create_job(conn, profile, stage="publish", source_id=source_id, media_key=KEY, data=data)
            return jobs.get(conn, job_id)  # type: ignore[return-value]

    def rows(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(sa.text("SELECT * FROM posts ORDER BY account_id")).mappings()]

    def state(self, job) -> str:
        with self.db.connect() as conn:
            return jobs.get(conn, job["id"])["status"]  # type: ignore[index]


@pytest.fixture
def world(db, tmp_path):
    with fake_google() as (fake, url):
        yield World(db, tmp_path, fake, url)


def test_an_approved_clip_is_uploaded_with_its_title_text_and_the_accounts_visibility(world):
    job = world.job()
    world.publisher(accounts_file(world.tmp, visibility="unlisted")).process(job)
    (row,) = world.rows()
    assert (row["account_id"], row["state"], row["remote_id"]) == ("yt_test", "posted", "vid123")
    assert row["remote_url"] == "https://www.youtube.com/watch?v=vid123" and row["posted_at"] is not None
    sent = world.fake.metadata[0]
    assert sent["snippet"]["title"] == "Judul klip" and sent["snippet"]["description"] == "Isi klip\n\n#mlbb"
    assert sent["status"]["privacyStatus"] == "unlisted"
    assert bytes(world.fake.uploads["s1"]) == world.store.path(KEY).read_bytes()


def test_only_the_youtube_accounts_are_touched_and_the_clip_waits_for_the_other_platforms(world):
    job = world.job()
    world.publisher().process(job)
    assert [r["account_id"] for r in world.rows()] == ["yt_test"] and world.state(job) == "queued"


def test_processing_a_clip_twice_uploads_it_once(world):
    job = world.job()
    publisher = world.publisher()
    publisher.process(job)
    publisher.process(job)
    assert len(world.fake.metadata) == 1 and len(world.rows()) == 1


def test_the_clip_is_finished_when_every_target_account_has_a_final_post(world):
    job = world.job(targets=["yt_test"])
    world.publisher().process(job)
    assert world.state(job) == "done"


def test_a_clip_that_was_stopped_midway_is_resumed_from_its_saved_session_not_started_again(world):
    job = world.job(targets=["yt_test"])
    world.fake.fail_puts_from = 1
    publisher = world.publisher()
    publisher.process(job)  # first chunk goes, then YouTube fails: the post stays `uploading` with its session saved
    (row,) = world.rows()
    assert row["state"] == "uploading" and row["session_url"] and row["attempts"] == 1
    world.fake.fail_puts_from = None
    publisher.process(job)  # a later pass (or a restarted service) carries on
    assert world.rows()[0]["state"] == "posted" and len(world.fake.metadata) == 1
    assert bytes(world.fake.uploads["s1"]) == world.store.path(KEY).read_bytes() and world.alerts == []


def test_a_session_that_is_gone_is_parked_with_an_alert_instead_of_risking_a_second_post(world):
    job = world.job(targets=["yt_test"])
    with world.db.begin() as conn:
        post, _ = posts.claim(conn, job["id"], "yt_test", "shorts")
        posts.record_session(conn, post["id"], f"{world.url}/upload/session/s99")
    world.publisher().process(job)
    (row,) = world.rows()
    assert row["state"] == "failed" and "duplicate" in row["error"] and len(world.fake.metadata) == 0
    assert any("yt_test" in a and "duplicate" in a for a in world.alerts)


def test_three_failed_attempts_end_in_a_failed_post_and_an_alert(world):
    job = world.job(targets=["yt_test"])
    world.fake.put_failures = 999
    publisher = world.publisher()
    for _ in range(3):
        publisher.process(job)
    (row,) = world.rows()
    assert row["state"] == "failed" and row["attempts"] == 3 and len(world.alerts) == 1
    assert world.state(job) == "done"  # failed is final: the clip is not retried forever


@pytest.mark.parametrize("reason", ["quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded"])
def test_a_limit_does_not_fail_the_post_it_makes_the_clip_wait_and_tells_you_once(world, reason):
    job = world.job(targets=["yt_test"])
    world.fake.init_error = (403, reason)
    world.publisher().process(job)
    (row,) = world.rows()
    assert row["state"] == "uploading" and row["attempts"] == 0
    assert len(world.alerts) == 1 and "posting limit" in world.alerts[0] and reason in world.alerts[0]


def test_while_an_account_is_at_a_limit_no_other_clip_is_started_for_it(world):
    """A clip started now would sit as an upload that never sent anything, using up the account's cap and spacing."""
    first, second = world.job(targets=["yt_test"]), world.job(targets=["yt_test"])
    world.fake.init_error = (403, "uploadLimitExceeded")
    publisher = world.publisher()
    publisher.process(first)
    publisher.process(second)
    assert len(world.rows()) == 1 and len(world.alerts) == 1


def test_after_a_limit_the_account_is_left_alone_for_hours_and_asked_again_after_that(world):
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()
    job = world.job(targets=["yt_test"])
    world.fake.init_error = (403, "uploadLimitExceeded")
    publisher = world.publisher(clock=clock)
    publisher.process(job)
    asked = len(world.fake.calls)
    publisher.process(job)
    publisher.process(job)
    assert len(world.fake.calls) == asked and len(world.alerts) == 1  # not asked every minute, not alerted every minute
    clock.now += 5 * 3600
    publisher.process(job)
    assert len(world.fake.calls) == asked  # still inside the 6 hours
    clock.now += 2 * 3600
    world.fake.init_error = None
    publisher.process(job)
    assert world.rows()[0]["state"] == "posted"  # nothing was lost


def test_an_expired_sign_in_keeps_the_clip_waiting_and_tells_you_to_sign_in_again(world):
    job = world.job(targets=["yt_test"])
    world.token_error = g.AuthExpired("Google no longer accepts the saved sign-in: run the authorize command again")
    world.publisher().process(job)
    (row,) = world.rows()
    assert row["state"] == "uploading" and row["attempts"] == 0 and "authorize" in world.alerts[0]
    world.token_error = None
    world.publisher().process(job)
    assert world.rows()[0]["state"] == "posted"  # after signing in again nothing was lost


def test_outside_the_posting_window_nothing_is_sent_and_nothing_is_recorded_until_the_window_opens(world):
    with world.db.connect() as conn:
        now = conn.execute(sa.text("SELECT clock_timestamp()")).scalar_one()
    later = (now + timedelta(hours=2)).astimezone().replace(minute=0, second=0, microsecond=0)
    window = f"{later:%H:%M}-{(later + timedelta(hours=1)):%H:%M}"
    path = accounts_file(world.tmp, post_windows=[window])
    job = world.job(targets=["yt_test"])
    world.clock = now
    publisher = world.publisher(path)
    publisher.process(job)
    assert world.rows() == [] and world.fake.metadata == [] and world.state(job) == "queued"
    world.clock = later + timedelta(minutes=10)
    publisher.process(job)
    assert world.rows()[0]["state"] == "posted"


def test_the_daily_cap_stops_a_second_clip_for_the_day(world):
    path = accounts_file(world.tmp, daily_cap=1)
    publisher = world.publisher(path)
    first = world.job(targets=["yt_test"])
    publisher.process(first)
    second = world.job(targets=["yt_test"])
    publisher.process(second)
    assert len(world.fake.metadata) == 1 and len(world.rows()) == 1 and world.state(second) == "queued"


def test_a_source_that_was_paused_after_a_claim_gets_no_upload(world):
    job = world.job(targets=["yt_test"])
    with world.db.begin() as conn:
        sources.pause(conn, job["source_id"], updated_by="test")
    world.publisher().process(job)
    (row,) = world.rows()
    assert row["state"] == "skipped" and "paused" in row["error"] and world.fake.metadata == []
    assert world.state(job) == "done"


def test_a_clip_whose_source_is_blocked_has_no_accounts_and_is_simply_finished(world):
    job = world.job(edit=lambda p: setattr(p.source, "clip_policy", "blocked"))
    world.publisher().process(job)
    assert world.rows() == [] and world.fake.metadata == [] and world.state(job) == "done"


def test_a_clip_missing_from_the_store_is_a_failed_post_with_an_alert(world):
    job = world.job(targets=["yt_test"])
    world.store.path(KEY).unlink()
    world.publisher().process(job)
    assert world.rows()[0]["state"] == "failed" and "not in the media store" in world.alerts[0]


def test_after_an_expired_sign_in_youtube_is_not_asked_again_until_the_pause_is_over(world):
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()
    job = world.job(targets=["yt_test"])
    world.token_error = g.AuthExpired("Google no longer accepts the saved sign-in: run the authorize command again")
    publisher = world.publisher(clock=clock)
    publisher.process(job)
    asked = world.token_calls
    publisher.process(job)
    assert world.token_calls == asked and len(world.alerts) == 1
    clock.now += 1801
    publisher.process(job)
    assert world.token_calls > asked
