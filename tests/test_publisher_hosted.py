"""Files kept in R2 for a while: what is stored, what you asked to delete, what has expired, what is gone."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import jobs, sources
from services.publisher import hosted, posts

pytestmark = pytest.mark.integration


@pytest.fixture
def post_id(db):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        job = jobs.create_job(conn, profiles[0], stage="publish")
        post, _ = posts.claim(conn, job, "tt_test", "tiktok")
        return post["id"]


def add(db, post_id, key="tiktok/a.mp4", size=1000, chats=None):
    with db.begin() as conn:
        return hosted.add(conn, post_id, key, size, chats or [{"chat_id": 111, "message_id": 5}])


def age(db, hours):
    with db.begin() as conn:
        conn.execute(
            sa.text("UPDATE hosted_files SET created_at = clock_timestamp() - make_interval(hours => :h)"), {"h": hours}
        )


def test_a_stored_file_is_remembered_with_its_size_and_where_you_were_told_about_it(db, post_id):
    file_id = add(db, post_id)
    with db.connect() as conn:
        file = hosted.get(conn, file_id)
    assert file is not None and (file.key, file.bytes, file.chats) == (
        "tiktok/a.mp4",
        1000,
        [{"chat_id": 111, "message_id": 5}],
    )


def test_storing_the_same_key_twice_is_one_file(db, post_id):
    assert add(db, post_id) == add(db, post_id)
    with db.connect() as conn:
        assert hosted.total_bytes(conn) == 1000


def test_the_total_counts_only_files_that_are_still_there(db, post_id):
    first, _second = add(db, post_id, "tiktok/a.mp4", 1000), add(db, post_id, "tiktok/b.mp4", 500)
    with db.begin() as conn:
        assert hosted.total_bytes(conn) == 1500
        hosted.mark_deleted(conn, first)
        assert hosted.total_bytes(conn) == 500


def test_a_delete_request_marks_the_file_once_and_only_while_it_exists(db, post_id):
    file_id = add(db, post_id)
    with db.begin() as conn:
        assert hosted.request_delete(conn, file_id) is True
        assert hosted.request_delete(conn, file_id) is False  # already asked
        hosted.mark_deleted(conn, file_id)
        assert hosted.request_delete(conn, file_id) is False  # already gone


def test_a_request_for_a_file_that_does_not_exist_is_refused(db):
    with db.begin() as conn:
        assert hosted.request_delete(conn, "00000000-0000-0000-0000-000000000000") is False


def test_what_you_asked_to_delete_and_what_is_too_old_is_due_and_the_rest_is_not(db, post_id):
    asked, old, fresh = (
        add(db, post_id, "tiktok/a.mp4"),
        add(db, post_id, "tiktok/b.mp4"),
        add(db, post_id, "tiktok/c.mp4"),
    )
    with db.begin() as conn:
        conn.execute(
            sa.text("UPDATE hosted_files SET created_at = clock_timestamp() - interval '80 hours' WHERE id = :i"),
            {"i": old},
        )
        hosted.request_delete(conn, asked)
    with db.connect() as conn:
        due = {f.id: why for f, why in hosted.due(conn, max_age_h=72)}
    assert due == {asked: "asked", old: "expired"} and fresh not in due


def test_a_deleted_file_is_not_due_again(db, post_id):
    file_id = add(db, post_id)
    age(db, 100)
    with db.begin() as conn:
        hosted.mark_deleted(conn, file_id)
    with db.connect() as conn:
        assert hosted.due(conn, max_age_h=72) == []


def test_the_table_belongs_to_a_tenant_like_every_other(db):
    with db.connect() as conn:
        cols = {
            r[0]
            for r in conn.execute(
                sa.text("SELECT column_name FROM information_schema.columns WHERE table_name = 'hosted_files'")
            )
        }
    assert {"tenant_id", "post_id", "key", "bytes", "chats", "delete_requested_at", "deleted_at"} <= cols
