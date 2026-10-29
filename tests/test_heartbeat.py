"""Every service says it is alive every half minute; a quiet service can be told from one that is just idle."""

from __future__ import annotations

import threading
import time

import pytest
import sqlalchemy as sa

from common import heartbeat

pytestmark = pytest.mark.integration


def age(db, service, seconds, instance="box-1"):
    with db.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE heartbeats SET beat_at = clock_timestamp() - make_interval(secs => :s) "
                "WHERE service = :n AND instance = :i"
            ),
            {"s": seconds, "n": service, "i": instance},
        )


def beat(db, service, instance="box-1"):
    with db.begin() as conn:
        heartbeat.beat(conn, service, instance)


def test_a_beat_is_one_row_per_service_and_instance_that_is_refreshed(db):
    beat(db, "select")
    beat(db, "select")
    beat(db, "select", "box-2")
    with db.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM heartbeats")).scalar_one() == 2


def test_a_service_that_beat_lately_is_up_one_that_did_not_is_down_and_one_never_seen_is_unknown(db):
    beat(db, "select")
    beat(db, "render")
    age(db, "render", 300)
    with db.connect() as conn:
        found = {s.name: s for s in heartbeat.states(conn, ["select", "render", "package"], stale_after_s=120)}
    assert found["select"].up is True and (found["select"].age_s or 99) < 5
    assert found["render"].up is False and 290 < (found["render"].age_s or 0) < 320
    assert found["package"].up is None and found["package"].age_s is None


def test_a_service_with_several_instances_is_up_while_any_of_them_beats(db):
    beat(db, "render", "box-1")
    beat(db, "render", "box-2")
    age(db, "render", 999, "box-1")
    with db.connect() as conn:
        assert heartbeat.states(conn, ["render"], stale_after_s=120)[0].up is True


def test_states_come_back_in_the_order_asked(db):
    with db.connect() as conn:
        assert [s.name for s in heartbeat.states(conn, ["b", "a", "c"], stale_after_s=60)] == ["b", "a", "c"]


def test_the_beat_thread_beats_at_once_keeps_beating_and_stops_when_told(db):
    stop = threading.Event()
    thread = heartbeat.start(db, "scaler", instance="t-1", every_s=0.05, stop=stop)
    time.sleep(0.4)
    with db.connect() as conn:
        first = conn.execute(sa.text("SELECT beat_at FROM heartbeats WHERE service = 'scaler'")).scalar_one()
    time.sleep(0.3)
    with db.connect() as conn:
        later = conn.execute(sa.text("SELECT beat_at FROM heartbeats WHERE service = 'scaler'")).scalar_one()
    stop.set()
    thread.join(2)
    assert later > first and not thread.is_alive()


def test_a_database_that_is_briefly_gone_does_not_kill_the_thread(db):
    class Flaky:
        def __init__(self):
            self.calls = 0

        def begin(self):
            self.calls += 1
            if self.calls == 1:
                raise sa.exc.OperationalError("x", {}, Exception("db is down"))
            return db.begin()

    stop = threading.Event()
    flaky = Flaky()
    thread = heartbeat.start(flaky, "scaler", instance="t-2", every_s=0.05, stop=stop)  # type: ignore[arg-type]
    time.sleep(0.5)
    stop.set()
    thread.join(2)
    assert flaky.calls >= 2
    with db.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM heartbeats WHERE instance = 't-2'")).scalar_one() == 1


def test_the_table_has_a_tenant_like_every_other(db):
    with db.connect() as conn:
        cols = {
            r[0]
            for r in conn.execute(
                sa.text("SELECT column_name FROM information_schema.columns WHERE table_name = 'heartbeats'")
            )
        }
    assert {"tenant_id", "service", "instance", "beat_at"} <= cols
