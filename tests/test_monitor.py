"""The watchdog: tells you once when a service goes quiet, once when it is back, and sends a digest now and then."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from common import heartbeat
from services.scaler.monitor import Monitor

pytestmark = pytest.mark.integration


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class World:
    def __init__(self, db):
        self.db, self.clock = db, Clock()
        self.alerts: list[str] = []
        self.posts: list[str] = []

    def monitor(self, expected=("select", "render"), **kw):
        return Monitor(
            self.db, list(expected), stale_after_s=120, alert=self.alerts.append, post=self.posts.append,
            digest_text=lambda: "the digest", digest_every_s=6 * 3600, grace_s=300, clock=self.clock, **kw,
        )  # fmt: skip

    def beat(self, service, ago=0):
        with self.db.begin() as conn:
            heartbeat.beat(conn, service, "box-1")
            conn.execute(
                sa.text(
                    "UPDATE heartbeats SET beat_at = clock_timestamp() - make_interval(secs => :s) WHERE service = :n"
                ),
                {"s": ago, "n": service},
            )


@pytest.fixture
def world(db):
    return World(db)


def test_a_service_that_goes_quiet_is_reported_once_with_how_long_it_has_been(world):
    world.beat("select")
    world.beat("render", ago=400)
    monitor = world.monitor()
    monitor.check()
    monitor.check()
    assert len(world.alerts) == 1 and "render" in world.alerts[0] and "7 min" in world.alerts[0]


def test_when_it_comes_back_the_monitoring_room_is_told_once(world):
    world.beat("select")
    world.beat("render", ago=400)
    monitor = world.monitor()
    monitor.check()
    world.beat("render")
    monitor.check()
    monitor.check()
    assert len(world.alerts) == 1 and len(world.posts) == 1 and "render" in world.posts[0] and "back" in world.posts[0]


def test_services_that_all_beat_say_nothing(world):
    world.beat("select")
    world.beat("render")
    world.monitor().check()
    assert world.alerts == [] and world.posts == []


def test_a_service_never_seen_is_given_time_to_start_then_reported(world):
    world.beat("select")  # render never reports
    monitor = world.monitor()
    monitor.check()
    assert world.alerts == []  # the watchdog itself has only just started
    world.clock.now += 301
    monitor.check()
    assert len(world.alerts) == 1 and "render" in world.alerts[0] and "never" in world.alerts[0]


def test_a_service_that_was_down_before_the_watchdog_started_is_reported_after_the_grace(world):
    world.beat("select")
    world.beat("render", ago=900)
    monitor = world.monitor()
    monitor.check()
    assert len(world.alerts) == 1  # it was seen before, so it is known to be down at once


def test_the_digest_is_sent_when_due_and_not_before(world):
    world.beat("select")
    world.beat("render")
    monitor = world.monitor()
    monitor.check()
    assert world.posts == []  # not due yet
    world.clock.now += 6 * 3600 + 1
    world.beat("select")
    world.beat("render")
    monitor.check()
    assert world.posts == ["the digest"]
    monitor.check()
    assert world.posts == ["the digest"]


def test_a_failing_notifier_does_not_stop_the_check(world):
    world.beat("render", ago=400)

    def broken(message):
        raise ConnectionError("telegram is down")

    monitor = Monitor(
        world.db, ["render"], stale_after_s=120, alert=broken, post=broken, digest_text=lambda: "d",
        digest_every_s=10, grace_s=0, clock=world.clock,
    )  # fmt: skip
    monitor.check()
    monitor.check()  # no exception
