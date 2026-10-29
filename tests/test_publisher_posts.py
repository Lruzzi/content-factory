"""One row per clip per account, written before anything is sent, so a redelivery cannot post twice (FR-9)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from db_helpers import CHECK, UNIQUE, violation

import config_schema as cs
from common import jobs, sources
from services.publisher import posts

pytestmark = pytest.mark.integration


@pytest.fixture
def job_id(db):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        profile = next(p for p in profiles if p.source.id == "leokabuki")
        return jobs.create_job(conn, profile, stage="publish")


def claim(db, job, account="yt_test", platform="shorts"):
    with db.begin() as conn:
        return posts.claim(conn, job, account, platform)


def test_claiming_writes_an_uploading_row_with_a_key_that_names_the_clip_and_the_account(db, job_id):
    post, is_new = claim(db, job_id)
    assert is_new and post["state"] == "uploading" and post["idempotency_key"] == f"{job_id}:yt_test"
    assert post["attempts"] == 0 and post["session_url"] is None


def test_claiming_twice_returns_the_same_row_and_says_it_is_not_new(db, job_id):
    first, _ = claim(db, job_id)
    again, is_new = claim(db, job_id)
    assert not is_new and again["id"] == first["id"]


def test_each_account_gets_its_own_row(db, job_id):
    claim(db, job_id, "yt_test")
    claim(db, job_id, "tt_test", "tiktok")
    with db.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM posts")).scalar_one() == 2


def test_the_database_itself_refuses_a_second_row_or_a_made_up_state(db, job_id):
    claim(db, job_id)
    with violation(UNIQUE), db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO posts (job_id, account_id, platform, idempotency_key) "
                "VALUES (:j, 'yt_test', 'shorts', 'x')"
            ),
            {"j": job_id},
        )
    with violation(CHECK), db.begin() as conn:
        conn.execute(sa.text("UPDATE posts SET state = 'maybe'"))


def test_the_upload_session_is_remembered_so_a_crashed_upload_can_be_resumed(db, job_id):
    post, _ = claim(db, job_id)
    with db.begin() as conn:
        posts.record_session(conn, post["id"], "https://upload.example/session/1")
    again, _ = claim(db, job_id)
    assert again["session_url"] == "https://upload.example/session/1"


def test_a_posted_clip_keeps_where_it_went(db, job_id):
    post, _ = claim(db, job_id)
    with db.begin() as conn:
        posts.mark_posted(conn, post["id"], "vid123", "https://www.youtube.com/watch?v=vid123")
    done, _ = claim(db, job_id)
    assert (done["state"], done["remote_id"], done["remote_url"]) == (
        "posted",
        "vid123",
        "https://www.youtube.com/watch?v=vid123",
    )
    assert done["posted_at"] is not None


def test_failures_are_counted_and_the_third_one_is_final(db, job_id):
    post, _ = claim(db, job_id)
    with db.begin() as conn:
        assert posts.record_failure(conn, post["id"], "HTTP 503", max_attempts=3) == "uploading"
        assert posts.record_failure(conn, post["id"], "HTTP 503", max_attempts=3) == "uploading"
        assert posts.record_failure(conn, post["id"], "HTTP 503", max_attempts=3) == "failed"
    final, _ = claim(db, job_id)
    assert final["state"] == "failed" and final["attempts"] == 3 and "503" in final["error"]


def test_a_clip_that_is_skipped_says_why(db, job_id):
    with db.begin() as conn:
        posts.skip(conn, job_id, "yt_test", "shorts", "the source is paused")
    row, is_new = claim(db, job_id)
    assert not is_new and row["state"] == "skipped" and row["error"] == "the source is paused"


def test_only_posts_that_were_started_count_toward_an_accounts_cap_and_spacing(db, job_id):
    claim(db, job_id)
    with db.begin() as conn:
        posts.skip(conn, job_id, "yt_other", "shorts", "the source is paused")
    with db.connect() as conn:
        times = posts.recent_times(conn, "yt_test")
        assert len(times) == 1 and times[0].tzinfo is not None
        assert posts.recent_times(conn, "yt_other") == []  # a skipped clip was never posted


def test_a_clip_is_finished_only_when_every_target_account_has_a_final_post(db, job_id):
    accounts = ["yt_test", "tt_test"]
    with db.connect() as conn:
        assert not posts.all_final(conn, job_id, accounts)
    post, _ = claim(db, job_id)
    with db.begin() as conn:
        posts.mark_posted(conn, post["id"], "v", "u")
    with db.connect() as conn:
        assert not posts.all_final(conn, job_id, accounts)  # tt_test has nothing yet
    with db.begin() as conn:
        posts.skip(conn, job_id, "tt_test", "tiktok", "no publisher yet")
    with db.connect() as conn:
        assert posts.all_final(conn, job_id, accounts)
