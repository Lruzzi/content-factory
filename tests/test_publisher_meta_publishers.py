"""Posting an approved clip as a Facebook Page Reel and an Instagram Reel: resuming, no double posts, alerts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import yaml
from fake_meta_server import fake_meta

import config_schema as cs
from common import jobs, sources
from common.media import LocalStore
from services.publisher import accounts as acc
from services.publisher import posts
from services.publisher.facebook import FacebookPublisher
from services.publisher.instagram import InstagramPublisher
from services.publisher.meta import Meta

pytestmark = pytest.mark.integration

KEY = "clips/real.mp4"
ENV = {
    "FB_TEST_PAGE_TOKEN": "page-token-1",
    "FB_TEST_PAGE_ID": "PAGE1",
    "IG_TEST_TOKEN": "ig-token-1",
    "IG_TEST_USER_ID": "IG1",
}  # noqa: S105  # gitleaks:allow


def accounts_file(tmp_path: Path) -> Path:
    defaults = {"daily_cap": 5, "min_spacing_min": 0, "post_windows": [], "visibility": "public"}
    rows = [
        {"id": "yt_test", "platform": "shorts", "test": True, "credentials": {}},
        {"id": "tt_test", "platform": "tiktok", "test": True, "credentials": {}},
        {"id": "ig_test", "platform": "reels", "test": True,
         "credentials": {"token_env": "IG_TEST_TOKEN", "ig_user_id_env": "IG_TEST_USER_ID"}},
        {"id": "fb_test", "platform": "facebook", "test": True,
         "credentials": {"token_env": "FB_TEST_PAGE_TOKEN", "page_id_env": "FB_TEST_PAGE_ID"}},
    ]  # fmt: skip
    path = tmp_path / "accounts.yaml"
    path.write_text(yaml.safe_dump({"timezone": "Asia/Jakarta", "defaults": defaults, "accounts": rows}))
    return path


class World:
    def __init__(self, db, tmp_path, fake, url):
        self.db, self.tmp, self.fake, self.url = db, tmp_path, fake, url
        self.store = LocalStore(tmp_path / "media")
        path = self.store.path(KEY)
        path.parent.mkdir(parents=True)
        path.write_bytes(bytes(range(256)) * 400)
        self.alerts: list[str] = []
        self.env = dict(ENV)

    def meta_for(self, rules: acc.AccountRules) -> Meta:
        return Meta(self.env[rules.credentials["token_env"]], graph_url=self.url, rupload_url=self.url, retry_wait_s=0)

    def make(self, cls, **kw: Any):
        return cls(
            self.db, self.store, acc.load(accounts_file(self.tmp)), self.meta_for, env=self.env,
            alert=self.alerts.append, poll_s=0, polls=3, **kw,
        )  # fmt: skip

    def job(self, targets, duration=26.7) -> jobs.Job:
        with self.db.begin() as conn:
            sources.seed_from_yaml(conn, cs.ROOT)
            profiles, _ = sources.load_profiles(conn, cs.ROOT)
            profile = next(p for p in profiles if p.source.id == "leokabuki")
            profile.test_targets = [cs.Target(account=t) for t in targets]
            source_id = conn.execute(sa.text("SELECT id FROM sources WHERE source_key = 'leokabuki'")).scalar_one()
            data = {
                "render": {"output": {"key": KEY, "duration_s": duration}},
                "package": {
                    "facebook": {"title": "Judul FB", "text": "Isi FB #mlbb", "hashtags": ["#mlbb"]},
                    "reels": {"title": "Judul IG", "text": "Judul IG\n\nIsi IG #mlbb", "hashtags": ["#mlbb"]},
                },
            }
            job_id = jobs.create_job(conn, profile, stage="publish", source_id=source_id, media_key=KEY, data=data)
            return jobs.get(conn, job_id)  # type: ignore[return-value]

    def rows(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(sa.text("SELECT * FROM posts ORDER BY account_id")).mappings()]

    def seed_post(self, job, account, platform, session):
        with self.db.begin() as conn:
            post, _ = posts.claim(conn, job["id"], account, platform)
            if session:
                posts.record_session(conn, post["id"], session)


@pytest.fixture
def world(db, tmp_path):
    with fake_meta() as (fake, url):
        yield World(db, tmp_path, fake, url)


# ======================= Facebook =======================
def test_a_clip_becomes_a_facebook_reel_with_the_facebook_text_and_comes_back_with_its_link(world):
    job = world.job(["fb_test"])
    world.make(FacebookPublisher).process(job)
    (row,) = world.rows()
    assert (row["account_id"], row["state"], row["remote_id"]) == ("fb_test", "posted", "fbv1")
    assert row["remote_url"] == "https://www.facebook.com/reel/fbv1" and row["session_url"] == "fbv1|finished"
    finish = world.fake.fb_finish[0]
    assert (
        finish["description"] == "Isi FB #mlbb"
        and finish["title"] == "Judul FB"
        and finish["video_state"] == "PUBLISHED"
    )
    assert bytes(world.fake.fb_received["fbv1"]) == world.store.path(KEY).read_bytes()
    assert all(c.headers["Authorization"] == "OAuth page-token-1" for c in world.fake.calls)
    assert (
        world.fake.calls[0].path == "/v26.0/PAGE1/video_reels"
    )  # the Page id comes from the env var the account names


def test_processing_twice_posts_once(world):
    job = world.job(["fb_test"])
    publisher = world.make(FacebookPublisher)
    publisher.process(job)
    publisher.process(job)
    assert len(world.fake.fb_finish) == 1 and len(world.rows()) == 1


def test_an_upload_that_stopped_halfway_carries_on_from_the_saved_reel_and_starts_nothing_new(world):
    job = world.job(["fb_test"])
    world.fake.fb_received["fbv1"] = bytearray(world.store.path(KEY).read_bytes()[:30_000])  # a start the service saved
    world.fake.file_size = world.store.path(KEY).stat().st_size
    world.seed_post(job, "fb_test", "facebook", "fbv1")
    world.make(FacebookPublisher).process(job)
    assert world.rows()[0]["state"] == "posted"
    assert bytes(world.fake.fb_received["fbv1"]) == world.store.path(KEY).read_bytes()
    assert world.fake.calls_to("POST", r"/v26.0/PAGE1/video_reels") and len(world.fake.fb_finish) == 1
    assert [c.form.get("upload_phase") for c in world.fake.calls_to("POST", r"/v26.0/PAGE1/video_reels")] == ["finish"]


def test_a_reel_already_finished_is_not_finished_again(world):
    job = world.job(["fb_test"])
    world.fake.fb_received["fbv1"] = bytearray(world.store.path(KEY).read_bytes())
    world.fake.file_size = len(world.fake.fb_received["fbv1"])
    world.fake.fb_published.add("fbv1")  # the crash came after Facebook published it
    world.seed_post(job, "fb_test", "facebook", "fbv1|finished")
    world.make(FacebookPublisher).process(job)
    assert world.rows()[0]["state"] == "posted" and world.fake.fb_finish == []


def test_a_reel_facebook_is_still_processing_is_waited_for_not_failed_and_not_finished_twice(world):
    job = world.job(["fb_test"])
    world.fake.fb_received["fbv1"] = bytearray(world.store.path(KEY).read_bytes())
    world.fake.file_size = len(world.fake.fb_received["fbv1"])
    world.seed_post(job, "fb_test", "facebook", "fbv1|finished")  # finished, but Facebook has not published it yet
    world.make(FacebookPublisher).process(job)
    row = world.rows()[0]
    assert row["state"] == "uploading" and row["attempts"] == 0 and world.fake.fb_finish == []


NOT_THERE = (400, {"error": {"message": "Unsupported get request. Object with ID 'fbv1' does not exist", "code": 100}})


def test_a_reel_facebook_cannot_read_yet_right_after_it_was_finished_is_waited_for_not_failed(world):
    """Seen on 2026-10-28: the Reel was published, but Facebook answered "object does not exist" for a moment."""
    job = world.job(["fb_test"])
    world.fake.fb_received["fbv1"] = bytearray(world.store.path(KEY).read_bytes())
    world.fake.file_size = len(world.fake.fb_received["fbv1"])
    world.seed_post(job, "fb_test", "facebook", "fbv1|finished")
    world.fake.errors["GET /v26.0/fbv1"] = NOT_THERE
    world.make(FacebookPublisher).process(job)
    row = world.rows()[0]
    assert row["state"] == "uploading" and row["attempts"] == 0 and not world.alerts and world.fake.fb_finish == []
    world.fake.errors.clear()
    world.fake.fb_published.add("fbv1")
    world.make(FacebookPublisher).process(job)
    assert world.rows()[0]["state"] == "posted"  # the next sweep finds it, and the Reel was never finished twice
    assert world.fake.fb_finish == []


def test_the_same_answer_while_the_reel_is_being_made_is_a_real_error(world):
    job = world.job(["fb_test"])
    world.fake.errors["GET /v26.0/fbv1"] = NOT_THERE  # not finished yet: Facebook should know the id it just gave
    world.make(FacebookPublisher).process(job)
    row = world.rows()[0]
    assert row["state"] == "failed" and "does not exist" in row["error"]


def test_an_expired_token_keeps_the_clip_waiting_and_names_the_command_that_fixes_it(world):
    job = world.job(["fb_test"])
    world.fake.errors["POST /v26.0/PAGE1/video_reels"] = (
        400,
        {"error": {"message": "Session has expired", "code": 190}},
    )
    world.make(FacebookPublisher).process(job)
    assert world.rows()[0]["state"] == "uploading" and world.rows()[0]["attempts"] == 0
    assert "meta-tokens" in world.alerts[0] and "fb_test" in world.alerts[0]


def test_a_temporary_facebook_error_is_counted_and_the_third_ends_it_with_an_alert(world):
    job = world.job(["fb_test"])
    world.fake.errors["POST /v26.0/PAGE1/video_reels"] = (500, {"error": {"message": "try later", "code": 2}})
    publisher = world.make(FacebookPublisher)
    for _ in range(3):
        publisher.process(job)
    assert world.rows()[0]["state"] == "failed" and world.rows()[0]["attempts"] == 3 and len(world.alerts) == 1


def test_a_refusal_that_trying_again_cannot_fix_fails_at_once(world):
    job = world.job(["fb_test"])
    world.fake.errors["POST /v26.0/PAGE1/video_reels"] = (
        400,
        {"error": {"message": "Aspect ratio not supported", "code": 100}},
    )
    world.make(FacebookPublisher).process(job)
    assert world.rows()[0]["state"] == "failed" and "Aspect ratio" in world.alerts[0]


def test_a_clip_longer_than_the_tested_limit_is_refused_before_anything_is_sent(world):
    job = world.job(["fb_test"], duration=400.0)
    world.make(FacebookPublisher).process(job)
    assert world.rows()[0]["state"] == "failed" and "300" in world.alerts[0] and world.fake.calls == []


def test_a_clip_of_three_minutes_goes_through_because_facebook_took_a_five_minute_reel(world):
    # tested for real on 2026-10-24: a 2 min and a 5 min Reel were published; Meta's docs still say 3-90 s
    job = world.job(["fb_test"], duration=180.0)
    world.make(FacebookPublisher).process(job)
    assert world.rows()[0]["state"] == "posted"


def test_a_missing_credential_names_the_variable_to_set(world):
    job = world.job(["fb_test"])
    del world.env["FB_TEST_PAGE_ID"]
    world.make(FacebookPublisher).process(job)
    assert "FB_TEST_PAGE_ID" in world.alerts[0] and world.fake.calls == []


# ======================= Instagram =======================
def test_a_clip_becomes_an_instagram_reel_with_the_instagram_caption_and_comes_back_with_its_link(world):
    job = world.job(["ig_test"])
    world.fake.polls_until_finished = 1
    world.make(InstagramPublisher).process(job)
    (row,) = world.rows()
    assert (row["account_id"], row["state"], row["remote_id"]) == ("ig_test", "posted", "igm1")
    assert row["remote_url"] == "https://www.instagram.com/reel/igm1/" and row["session_url"] == "igc1"
    created = world.fake.ig_created[0]
    assert created["caption"] == "Judul IG\n\nIsi IG #mlbb" and created["media_type"] == "REELS"
    assert bytes(world.fake.ig_received["igc1"]) == world.store.path(KEY).read_bytes()
    assert (
        world.fake.calls[0].path == "/v26.0/IG1/media"
        and world.fake.calls[0].headers["Authorization"] == "OAuth ig-token-1"
    )


def test_instagram_processing_twice_posts_once(world):
    job = world.job(["ig_test"])
    publisher = world.make(InstagramPublisher)
    publisher.process(job)
    publisher.process(job)
    assert len(world.fake.ig_created) == 1 and len(world.fake.ig_media) == 1


def test_a_saved_container_is_published_without_creating_or_uploading_again(world):
    job = world.job(["ig_test"])
    world.fake.ig_containers["igc1"] = {"status_code": "FINISHED", "polls": 0, "uploaded": True}
    world.seed_post(job, "ig_test", "reels", "igc1")
    world.make(InstagramPublisher).process(job)
    assert world.rows()[0]["state"] == "posted" and world.fake.ig_created == [] and len(world.fake.ig_media) == 1


def test_a_reel_that_was_published_just_before_a_crash_is_found_not_published_again(world):
    job = world.job(["ig_test"])
    world.fake.ig_containers["igc1"] = {"status_code": "PUBLISHED", "polls": 0, "uploaded": True}
    world.fake.ig_media.append(
        {"id": "igm7", "permalink": "https://www.instagram.com/reel/abc/", "timestamp": "2099-01-01T00:00:00+0000"}
    )
    world.seed_post(job, "ig_test", "reels", "igc1")
    world.make(InstagramPublisher).process(job)
    row = world.rows()[0]
    assert (row["state"], row["remote_id"], row["remote_url"]) == (
        "posted",
        "igm7",
        "https://www.instagram.com/reel/abc/",
    )
    assert world.fake.calls_to("POST", r"/v26.0/IG1/media_publish") == []


def test_a_published_reel_that_cannot_be_found_is_parked_with_an_alert_instead_of_guessing(world):
    job = world.job(["ig_test"])
    world.fake.ig_containers["igc1"] = {"status_code": "PUBLISHED", "polls": 0, "uploaded": True}
    world.seed_post(job, "ig_test", "reels", "igc1")
    world.make(InstagramPublisher).process(job)
    assert world.rows()[0]["state"] == "failed" and "Instagram" in world.alerts[0]


def test_a_reel_instagram_is_still_processing_is_waited_for_not_failed(world):
    job = world.job(["ig_test"])
    world.fake.polls_until_finished = 99
    world.make(InstagramPublisher).process(job)
    row = world.rows()[0]
    assert row["state"] == "uploading" and row["attempts"] == 0 and row["session_url"] == "igc1"
    world.fake.polls_until_finished = 0
    world.make(InstagramPublisher).process(job)
    assert world.rows()[0]["state"] == "posted" and len(world.fake.ig_created) == 1  # the same container, no new one


@pytest.mark.parametrize("code", ["ERROR", "EXPIRED"])
def test_a_container_that_failed_or_expired_ends_the_post_with_an_alert(world, code):
    job = world.job(["ig_test"])
    world.fake.ig_containers["igc1"] = {"status_code": code, "polls": 0, "uploaded": True}
    world.seed_post(job, "ig_test", "reels", "igc1")
    world.make(InstagramPublisher).process(job)
    assert world.rows()[0]["state"] == "failed" and code in world.alerts[0]


def test_an_expired_instagram_token_keeps_the_clip_waiting(world):
    job = world.job(["ig_test"])
    world.fake.errors["POST /v26.0/IG1/media"] = (400, {"error": {"message": "Session has expired", "code": 190}})
    world.make(InstagramPublisher).process(job)
    assert world.rows()[0]["state"] == "uploading" and "meta-tokens" in world.alerts[0]


def test_each_platform_only_touches_its_own_accounts(world):
    job = world.job(["fb_test", "ig_test"])
    world.make(FacebookPublisher).process(job)
    assert [r["account_id"] for r in world.rows()] == ["fb_test"]
    world.make(InstagramPublisher).process(job)
    assert [r["account_id"] for r in world.rows()] == ["fb_test", "ig_test"]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_after_a_sign_in_failure_the_account_is_left_alone_for_a_while_not_retried_every_minute(world):
    job = world.job(["fb_test"])
    world.fake.errors["POST /v26.0/PAGE1/video_reels"] = (
        400,
        {"error": {"message": "Session has expired", "code": 190}},
    )
    clock = Clock()
    publisher = world.make(FacebookPublisher, clock=clock)
    publisher.process(job)
    contacts = len(world.fake.calls)
    assert contacts >= 1 and len(world.alerts) == 1
    publisher.process(job)
    publisher.process(job)
    assert len(world.fake.calls) == contacts  # a blocked app is not asked again every minute
    clock.now += 1801
    publisher.process(job)
    assert len(world.fake.calls) > contacts  # after the pause it tries again


def test_a_spam_rate_limit_makes_the_clip_wait_for_hours_instead_of_failing_it(world):
    job = world.job(["fb_test"])
    world.fake.errors["POST /v26.0/PAGE1/video_reels"] = (
        400,
        {"error": {"message": "We limit how often you can post, comment or do other things", "code": 368}},
    )
    clock = Clock()
    publisher = world.make(FacebookPublisher, clock=clock)
    publisher.process(job)
    contacts = len(world.fake.calls)
    assert len(world.alerts) == 1 and "posting limit" in world.alerts[0]
    (row,) = world.rows()
    assert row["state"] == "uploading" and row["attempts"] == 0
    clock.now += 3600
    publisher.process(job)
    assert len(world.fake.calls) == contacts and len(world.alerts) == 1
    clock.now += 6 * 3600
    world.fake.errors.clear()
    publisher.process(job)
    assert len(world.fake.calls) > contacts


def test_the_pause_is_per_account_so_another_platforms_posts_carry_on(world):
    job = world.job(["fb_test", "ig_test"])
    world.fake.errors["POST /v26.0/PAGE1/video_reels"] = (
        400,
        {"error": {"message": "Session has expired", "code": 190}},
    )
    clock = Clock()
    world.make(FacebookPublisher, clock=clock).process(job)
    world.make(InstagramPublisher, clock=clock).process(job)
    assert [r["account_id"] for r in world.rows() if r["state"] == "posted"] == ["ig_test"]


# ======================= Instagram through a signed link (R2) =======================
from fake_s3_server import fake_s3  # noqa: E402

from common.r2 import R2  # noqa: E402
from services.publisher.clipstore import ClipStore  # noqa: E402

HOST = acc.MediaHost(
    bucket_env="B", account_id_env="A", access_key_env="K", secret_key_env="S",  # noqa: S106
    link_ttl_min=2880, keep_after_approval_h=24, max_age_h=48, max_total_gb=8,
)  # fmt: skip


class R2World:
    """The Instagram world plus an S3-compatible store holding the clip, as the review leaves it."""

    def __init__(self, world, s3, s3_url):
        self.world, self.s3 = world, s3
        secret = "secret-for-tests"  # noqa: S105
        self.r2 = R2(account_id="acct", bucket="clips", access_key="ak", secret_key=secret, endpoint=s3_url)
        self.clips = ClipStore(world.db, self.r2, HOST)

    def publisher(self, **kw):
        return self.world.make(InstagramPublisher, clips=self.clips, **kw)

    def keys(self):
        return sorted(k for k in self.s3.objects if k.startswith("clips/review/"))

    def puts(self):
        return [c for c in self.s3.calls if c[0] == "PUT"]


@pytest.fixture
def r2world(world):
    with fake_s3() as (s3, url):
        yield R2World(world, s3, url)


def test_the_clip_is_given_to_instagram_as_a_signed_link_and_not_uploaded_to_it(r2world):
    world = r2world.world
    job = world.job(["ig_test"])
    r2world.publisher().process(job)
    (row,) = world.rows()
    assert (row["state"], row["remote_id"]) == ("posted", "igm1") and row["session_url"] == "igc1"
    created = world.fake.ig_created[0]
    assert created["media_type"] == "REELS" and created["video_url"].startswith("http")
    assert "X-Amz-Signature" in created["video_url"] and f"review/{job['id']}.mp4" in created["video_url"]
    assert "upload_type" not in created  # not a resumable upload
    assert bytes(world.fake.ig_received["igc1"]) == b""  # nothing was sent to Instagram's upload host


def test_the_copy_stored_for_review_is_the_one_instagram_fetches_and_it_is_left_for_its_own_rule(r2world):
    world = r2world.world
    job = world.job(["ig_test"])
    r2world.clips.put(job["id"], world.store.path(KEY), pending_h=24)  # what the review did
    r2world.publisher().process(job)
    assert len(r2world.puts()) == 1  # not uploaded a second time
    assert r2world.keys() == [f"clips/review/{job['id']}.mp4"]  # and not deleted by the publisher: others use it too


def test_a_copy_that_is_missing_is_stored_again_and_kept_for_the_set_time(r2world):
    world = r2world.world
    job = world.job(["ig_test"])
    r2world.publisher().process(job)  # nothing was stored for review (a clip that needed no approval, or swept)
    assert len(r2world.puts()) == 1 and len(r2world.keys()) == 1
    with world.db.connect() as conn:
        hours = conn.execute(
            sa.text("SELECT EXTRACT(EPOCH FROM keep_until - clock_timestamp()) / 3600 FROM hosted_files")
        ).scalar_one()
    assert hours == pytest.approx(24, abs=0.1)


def test_a_container_still_processing_is_waited_for_and_the_retry_does_not_upload_again(r2world):
    world = r2world.world
    job = world.job(["ig_test"])
    world.fake.polls_until_finished = 99  # more than the publisher waits in one go
    publisher = r2world.publisher()
    publisher.process(job)
    assert world.rows()[0]["state"] == "uploading" and len(r2world.keys()) == 1
    world.fake.polls_until_finished = 0
    publisher.process(job)
    assert world.rows()[0]["state"] == "posted" and len(world.fake.ig_created) == 1 and len(r2world.puts()) == 1


def test_a_store_that_is_down_is_a_retryable_failure_and_nothing_is_asked_of_instagram(r2world):
    world = r2world.world
    job = world.job(["ig_test"])
    r2world.s3.fail["PUT"] = 503
    r2world.publisher().process(job)
    assert world.fake.ig_created == []
    (row,) = world.rows()
    assert row["state"] != "posted"


def test_without_a_store_the_resumable_upload_is_used_as_before(world):
    job = world.job(["ig_test"])
    world.make(InstagramPublisher).process(job)
    assert "video_url" not in world.fake.ig_created[0] and world.fake.ig_created[0]["upload_type"] == "resumable"
