"""The one stored copy of a clip in R2: put when the clip goes for review, deleted when it is rejected or its review
expires, kept a set time once approved, shared by TikTok and Instagram."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from fake_s3_server import fake_s3

from common.r2 import R2
from services.publisher import accounts as acc
from services.publisher import hosted
from services.publisher.clipstore import ClipStore

pytestmark = pytest.mark.integration

JOB = "01a17675-b6c4-7266-909d-6e08bb20ee03"
KEY = f"review/{JOB}.mp4"
HOST = acc.MediaHost(
    bucket_env="B", account_id_env="A", access_key_env="K", secret_key_env="S",  # noqa: S106
    link_ttl_min=2880, keep_after_approval_h=24, max_age_h=48, max_total_gb=8,
)  # fmt: skip


@pytest.fixture
def world(db, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"v" * 5000)
    with fake_s3() as (s3, url):
        secret = "secret-for-tests"  # noqa: S105
        r2 = R2(account_id="acct", bucket="clips", access_key="ak", secret_key=secret, endpoint=url)
        yield ClipStore(db, r2, HOST), s3, clip, db


def row(db):
    with db.connect() as conn:
        return (
            conn.execute(
                sa.text(
                    "SELECT key, bytes, deleted_at, keep_until, "
                    "EXTRACT(EPOCH FROM keep_until - clock_timestamp()) / 3600 AS hours FROM hosted_files"
                )
            )
            .mappings()
            .one_or_none()
        )


def test_a_clip_for_review_is_stored_under_its_job_and_comes_with_a_signed_link(world):
    store, s3, clip, db = world
    link = store.put(JOB, clip, pending_h=24)
    assert link is not None and "X-Amz-Signature" in link and KEY in link
    assert s3.objects[f"clips/{KEY}"] == clip.read_bytes()
    r = row(db)
    assert r["key"] == KEY and r["bytes"] == 5000 and r["deleted_at"] is None
    assert r["hours"] == pytest.approx(25, abs=0.1)  # the review's 24 hours and an hour more


def test_a_clip_that_is_already_stored_is_not_uploaded_again(world):
    store, s3, clip, _ = world
    store.put(JOB, clip, pending_h=24)
    store.put(JOB, clip, pending_h=24)
    assert len([c for c in s3.calls if c[0] == "PUT"]) == 1


def test_rejecting_deletes_it_at_once_and_says_so_in_the_record(world):
    store, s3, clip, db = world
    store.put(JOB, clip, pending_h=24)
    store.discard(JOB)
    assert f"clips/{KEY}" not in s3.objects and row(db)["deleted_at"] is not None
    store.discard(JOB)  # again: nothing to do, no error


def test_approving_keeps_it_for_the_set_time_from_now(world):
    store, _, clip, db = world
    store.put(JOB, clip, pending_h=2)
    store.approved(JOB)
    assert row(db)["hours"] == pytest.approx(24, abs=0.1)


def test_the_sweep_deletes_what_is_due_and_leaves_the_rest(world):
    store, s3, clip, db = world
    store.put(JOB, clip, pending_h=24)
    assert store.sweep() == 0 and f"clips/{KEY}" in s3.objects
    with db.begin() as conn:
        hosted.set_keep(conn, KEY, -1)  # its time has passed
    assert store.sweep() == 1 and f"clips/{KEY}" not in s3.objects and row(db)["deleted_at"] is not None


def test_a_file_older_than_the_ceiling_goes_whatever_its_keep_time_says(world):
    store, s3, clip, db = world
    store.put(JOB, clip, pending_h=24)
    with db.begin() as conn:
        conn.execute(sa.text("UPDATE hosted_files SET created_at = clock_timestamp() - interval '49 hours'"))
    assert store.sweep() == 1 and f"clips/{KEY}" not in s3.objects


def test_a_delete_that_fails_is_tried_again_by_the_next_sweep(world):
    store, s3, clip, db = world
    store.put(JOB, clip, pending_h=24)
    s3.fail["DELETE"] = 503
    store.discard(JOB)
    assert f"clips/{KEY}" in s3.objects and row(db)["deleted_at"] is None  # still there, and due
    del s3.fail["DELETE"]
    assert store.sweep() == 1 and f"clips/{KEY}" not in s3.objects


def test_a_store_that_is_down_means_no_link_not_an_error_for_the_review(world):
    store, s3, clip, db = world
    s3.fail["PUT"] = 503
    assert store.put(JOB, clip, pending_h=24) is None and row(db) is None


def test_a_full_store_means_no_link_either(world):
    store, s3, clip, _ = world
    small = ClipStore(store.engine, store.r2, acc.MediaHost(**{**HOST.__dict__, "max_total_gb": 0.000001}))
    assert small.put(JOB, clip, pending_h=24) is None and s3.objects == {}


def test_a_link_can_be_made_again_for_a_stored_clip_and_not_for_a_deleted_one(world):
    store, _, clip, _ = world
    store.put(JOB, clip, pending_h=24)
    assert store.link(JOB) is not None
    store.discard(JOB)
    assert store.link(JOB) is None


def test_the_publishers_store_it_again_when_it_is_missing_and_reuse_it_when_it_is_there(world):
    store, s3, clip, db = world
    store.store(JOB, clip, keep_h=24)  # nothing there: it is uploaded
    store.store(JOB, clip, keep_h=24)  # there: not again
    assert len([c for c in s3.calls if c[0] == "PUT"]) == 1 and row(db)["hours"] == pytest.approx(24, abs=0.1)


def test_r2_says_whether_a_file_is_there(world):
    store, _, clip, _ = world
    assert store.r2.exists(KEY) is False
    store.put(JOB, clip, pending_h=24)
    assert store.r2.exists(KEY) is True
