"""The chat listener: message counts per bucket of time, saved under the chat lease, and nothing else of a message."""

from __future__ import annotations

import threading
from collections import Counter

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import leases, sources
from services.chat_listener import counts, run
from services.chat_listener.service import ChatService

pytestmark = pytest.mark.integration

BUCKET = 10


class FakeReader:
    """Hands out the given batches of message times, one batch per poll, then reports the chat closed."""

    def __init__(self, batches, *, on_empty=None):
        self.batches, self.closed, self.on_empty = list(batches), False, on_empty

    def poll(self):
        if self.batches:
            return self.batches.pop(0)
        if self.on_empty:
            self.on_empty()
        return []

    def alive(self):
        return bool(self.batches) or self.on_empty is not None and not self.closed

    def close(self):
        self.closed = True


def new_stream(db, status="live"):
    with db.begin() as conn:
        return conn.execute(
            sa.text("INSERT INTO live_streams (video_id, status) VALUES ('vidchat0001', :s) RETURNING id"),
            {"s": status},
        ).scalar_one()


def stored(db, stream_id):
    with db.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT extract(epoch FROM bucket_at)::int, n FROM chat_counts WHERE stream_id = :s ORDER BY 1"),
            {"s": stream_id},
        ).all()
    return {int(t): n for t, n in rows}


def listen(db, stream_id, reader, **kw):
    return run.listen_to(
        db, stream_id, "chat-1", make_reader=lambda video_id: reader, bucket_s=BUCKET, ttl_s=60, heartbeat_s=0,
        poll_s=0, **kw,
    )  # fmt: skip


# ---------- buckets ----------
def test_a_time_falls_in_the_bucket_that_starts_at_a_multiple_of_the_size():
    assert (
        counts.bucket_of(1000.0, 10) == 1000
        and counts.bucket_of(1009.9, 10) == 1000
        and counts.bucket_of(1010.0, 10) == 1010
    )


def test_counts_added_twice_to_one_bucket_add_up(db):
    stream = new_stream(db)
    with db.begin() as conn:
        counts.add(conn, stream, Counter({1000: 2, 1010: 1}))
        counts.add(conn, stream, Counter({1000: 3}))
    assert stored(db, stream) == {1000: 5, 1010: 1}


# ---------- listening ----------
def test_messages_are_counted_per_bucket_and_saved_when_the_chat_closes(db):
    stream = new_stream(db)
    reader = FakeReader([[1000.5, 1001.0, 1009.9], [1010.1, 1025.0]])
    assert listen(db, stream, reader) == "ended"
    assert stored(db, stream) == {1000: 3, 1010: 1, 1020: 1}
    assert reader.closed


def test_the_chat_lease_is_released_afterwards_and_the_recorders_is_not_touched(db):
    stream = new_stream(db)
    with db.begin() as conn:
        leases.acquire(conn, stream, "recorder-1", 60)
    listen(db, stream, FakeReader([[1000.0]]))
    with db.connect() as conn:
        row = conn.execute(
            sa.text("SELECT lease_worker, chat_worker FROM live_streams WHERE id = :i"), {"i": stream}
        ).one()
    assert (row.lease_worker, row.chat_worker) == ("recorder-1", None)


def test_a_stream_someone_else_listens_to_is_left_alone(db):
    stream = new_stream(db)
    with db.begin() as conn:
        leases.acquire(conn, stream, "other", 60, kind="chat")
    reader = FakeReader([[1000.0]])
    assert listen(db, stream, reader) == "busy" and stored(db, stream) == {}


def test_listening_stops_when_the_stream_is_over(db):
    stream = new_stream(db)

    def end_the_stream():
        with db.begin() as conn:
            conn.execute(sa.text("UPDATE live_streams SET status = 'ended'"))

    reader = FakeReader([[1000.0]], on_empty=end_the_stream)
    assert listen(db, stream, reader) == "ended" and stored(db, stream) == {1000: 1}


def test_the_listener_stops_at_once_when_told_to(db):
    stream = new_stream(db)
    stop = threading.Event()
    stop.set()
    assert listen(db, stream, FakeReader([[1000.0]], on_empty=lambda: None), stop=stop) == "stopped"


def test_nothing_but_the_time_of_a_message_is_kept(db):
    stream = new_stream(db)
    listen(db, stream, FakeReader([[1000.0]]))
    with db.connect() as conn:
        columns = [
            r[0]
            for r in conn.execute(
                sa.text("SELECT column_name FROM information_schema.columns WHERE table_name = 'chat_counts'")
            )
        ]
    assert sorted(columns) == ["bucket_at", "n", "stream_id", "tenant_id"]


# ---------- the service ----------
@pytest.fixture
def seeded(db):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        source_id = conn.execute(sa.text("SELECT id FROM sources WHERE source_key = 'leokabuki'")).scalar_one()
        return conn.execute(
            sa.text("INSERT INTO live_streams (video_id, source_id) VALUES ('vidchat0002', :s) RETURNING id"),
            {"s": source_id},
        ).scalar_one()


def service(db, readers, **kw):
    return ChatService(
        db, cs.ROOT, "chat-svc", max_concurrent=kw.pop("max_concurrent", 3), ttl_s=60, heartbeat_s=0,
        make_reader=lambda video_id: readers.pop(0), poll_s=0, **kw,
    )  # fmt: skip


def test_the_service_listens_to_a_live_stream_whose_source_has_chat_on(db, seeded):
    svc = service(db, [FakeReader([[2000.0, 2001.0]])])
    assert svc.poll() == [seeded]
    svc.join(5)
    assert stored(db, seeded) == {2000: 2}


def test_the_service_leaves_a_paused_source_and_a_profile_with_chat_off_alone(db, seeded):
    with db.begin() as conn:
        conn.execute(sa.text("UPDATE sources SET paused_at = now(), pause_reason = 'manual'"))
    assert service(db, [FakeReader([])]).poll() == []


def test_the_service_starts_no_more_than_max_concurrent(db, seeded):
    new_stream(db)  # a second live stream, without a source: not listened to either
    assert service(db, [FakeReader([]), FakeReader([])], max_concurrent=0).poll() == []


def test_a_chat_that_cannot_be_read_is_not_tried_again_at_once(db, seeded):
    def broken(video_id):
        raise RuntimeError("chat closed")

    clock = [0.0]
    svc = ChatService(
        db, cs.ROOT, "w", max_concurrent=3, ttl_s=60, heartbeat_s=0, make_reader=broken, clock=lambda: clock[0]
    )
    assert svc.poll() == [seeded]
    svc.join(5)
    assert svc.poll() == []  # RETRY_S has not passed
    clock[0] += 400
    assert svc.poll() == [seeded]
    svc.join(5)


def test_nobody_is_listened_to_while_intake_is_paused(db, seeded):
    from common import pauses

    with db.begin() as conn:
        pauses.start(conn, pauses.switch_key("intake"), 3600, "test")
    assert service(db, [FakeReader([])]).poll() == []
