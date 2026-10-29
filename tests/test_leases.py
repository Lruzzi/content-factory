"""Recorder and chat-listener leases, timed by the Postgres clock (FR-18, NFR-11)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from common import leases

pytestmark = pytest.mark.integration

TTL = 60


def new_stream(db, *, status="live", video_id="vid0001"):
    with db.begin() as conn:
        return conn.execute(
            sa.text("INSERT INTO live_streams (video_id, status) VALUES (:v, :s) RETURNING id"),
            {"v": video_id, "s": status},
        ).scalar_one()


def age_lease(db, stream_id, seconds):
    with db.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE live_streams SET lease_heartbeat_at = clock_timestamp() - make_interval(secs => :s) "
                "WHERE id = :id"
            ),
            {"s": seconds, "id": stream_id},
        )


def test_the_first_worker_gets_the_lease(db):
    stream = new_stream(db)
    with db.begin() as conn:
        assert leases.acquire(conn, stream, "w1", TTL)


def test_a_second_worker_cannot_take_a_live_lease(db):
    stream = new_stream(db)
    with db.begin() as conn:
        leases.acquire(conn, stream, "w1", TTL)
    with db.begin() as conn:
        assert not leases.acquire(conn, stream, "w2", TTL)


def test_the_holder_can_acquire_again_to_renew(db):
    stream = new_stream(db)
    with db.begin() as conn:
        leases.acquire(conn, stream, "w1", TTL)
    with db.begin() as conn:
        assert leases.acquire(conn, stream, "w1", TTL)


def test_a_heartbeat_keeps_the_lease_for_its_owner_only(db):
    stream = new_stream(db)
    with db.begin() as conn:
        leases.acquire(conn, stream, "w1", TTL)
    with db.begin() as conn:
        assert leases.heartbeat(conn, stream, "w1")
        assert not leases.heartbeat(conn, stream, "w2")


def test_an_expired_lease_can_be_taken_and_the_old_owner_learns_it_lost_it(db):
    stream = new_stream(db)
    with db.begin() as conn:
        leases.acquire(conn, stream, "w1", TTL)
    age_lease(db, stream, TTL + 30)
    with db.begin() as conn:
        assert leases.acquire(conn, stream, "w2", TTL)
    with db.begin() as conn:
        assert not leases.heartbeat(conn, stream, "w1")


def test_a_lease_that_is_not_yet_expired_cannot_be_taken(db):
    stream = new_stream(db)
    with db.begin() as conn:
        leases.acquire(conn, stream, "w1", TTL)
    age_lease(db, stream, TTL - 30)
    with db.begin() as conn:
        assert not leases.acquire(conn, stream, "w2", TTL)


def test_release_frees_the_lease_only_for_its_owner(db):
    stream = new_stream(db)
    with db.begin() as conn:
        leases.acquire(conn, stream, "w1", TTL)
        leases.release(conn, stream, "w2")
        assert not leases.acquire(conn, stream, "w2", TTL)
        leases.release(conn, stream, "w1")
        assert leases.acquire(conn, stream, "w2", TTL)


def test_the_reconciler_finds_live_streams_with_no_or_stale_lease(db):
    fresh = new_stream(db, video_id="fresh")
    stale = new_stream(db, video_id="stale")
    nobody = new_stream(db, video_id="nobody")
    ended = new_stream(db, video_id="ended", status="ended")
    with db.begin() as conn:
        for stream in (fresh, stale, ended):
            leases.acquire(conn, stream, "w1", TTL)
    age_lease(db, stale, TTL + 30)
    age_lease(db, ended, TTL + 30)
    with db.connect() as conn:
        assert set(leases.needing_a_worker(conn, TTL)) == {stale, nobody}


def test_a_lease_check_does_not_depend_on_the_callers_clock(db):
    # the SQL takes no timestamp argument at all: time comes from the database
    stream = new_stream(db)
    with db.begin() as conn:
        assert leases.acquire(conn, stream, "w1", TTL)
        stamp = conn.execute(sa.text("SELECT lease_heartbeat_at <= clock_timestamp() FROM live_streams")).scalar_one()
    assert stamp is True


# ---------- the chat listener's lease is its own, on the same stream ----------
def test_the_recorder_and_the_chat_listener_each_hold_their_own_lease_on_one_stream(db):
    stream = new_stream(db)
    with db.begin() as conn:
        assert leases.acquire(conn, stream, "rec", TTL)
        assert leases.acquire(conn, stream, "chat1", TTL, kind="chat")
        assert not leases.acquire(conn, stream, "chat2", TTL, kind="chat")  # a second listener is turned away
        assert leases.heartbeat(conn, stream, "chat1", kind="chat") and not leases.heartbeat(
            conn, stream, "rec", kind="chat"
        )
        assert leases.needing_a_worker(conn, TTL) == [] and leases.needing_a_worker(conn, TTL, kind="chat") == []


def test_a_stream_nobody_listens_to_needs_a_chat_worker_until_one_takes_it_and_again_when_it_goes_quiet(db):
    stream = new_stream(db)
    with db.begin() as conn:
        leases.acquire(conn, stream, "rec", TTL)  # recorded, but nobody reads its chat
        assert leases.needing_a_worker(conn, TTL, kind="chat") == [stream]
        leases.acquire(conn, stream, "chat1", TTL, kind="chat")
        assert leases.needing_a_worker(conn, TTL, kind="chat") == []
        conn.execute(
            sa.text("UPDATE live_streams SET chat_heartbeat_at = clock_timestamp() - make_interval(secs => 500)")
        )
        assert leases.needing_a_worker(conn, TTL, kind="chat") == [stream]
        leases.release(conn, stream, "chat1", kind="chat")
        assert leases.acquire(conn, stream, "chat2", TTL, kind="chat")
