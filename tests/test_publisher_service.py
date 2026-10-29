"""The publisher service loop: act on approved clips as they arrive, look again at the ones that had to wait, and never
let one bad clip stop the rest."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import jobs, pipeline, sources
from common.streams import Consumer, publish
from services.publisher.service import PublisherService, Throttle

pytestmark = pytest.mark.integration


class Spy:
    def __init__(self, error=None):
        self.seen: list = []
        self.error = error

    def process(self, job):
        self.seen.append(job["id"])
        if self.error:
            raise self.error


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def make_job(db):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
    profile = next(p for p in profiles if p.source.id == "leokabuki")

    def make(stage="publish", status="queued"):
        with db.begin() as conn:
            job_id = jobs.create_job(conn, profile, stage=stage)
            conn.execute(sa.text("UPDATE jobs SET status = :s WHERE id = :i"), {"s": status, "i": job_id})
            return job_id

    return make


def service(db, redis_client, prefix, spy, clock=None, **kw):
    consumer = Consumer(
        redis_client, pipeline.stream_for("publish", prefix), "publisher-shorts", "w1", visibility_timeout_s=30,
        dlq=f"{prefix}.dlq",
    )  # fmt: skip
    return PublisherService(db, consumer, spy, sweep_every_s=60, clock=clock or Clock(), **kw)


def pending(redis_client, prefix):
    return redis_client.xpending(pipeline.stream_for("publish", prefix), "publisher-shorts")["pending"]


def send(redis_client, prefix, job_id):
    publish(redis_client, pipeline.stream_for("publish", prefix), str(job_id))


def test_an_approved_clip_is_processed_and_the_message_is_acknowledged(db, redis_client, stream_prefix, make_job):
    spy, job_id = Spy(), make_job()
    send(redis_client, stream_prefix, job_id)
    service(db, redis_client, stream_prefix, spy).step()
    assert spy.seen.count(job_id) >= 1 and pending(redis_client, stream_prefix) == 0


def test_a_message_for_a_clip_that_is_not_waiting_to_be_published_is_acknowledged_and_ignored(
    db, redis_client, stream_prefix, make_job
):
    spy = Spy()
    for stage, status in (("render", "queued"), ("publish", "done"), ("publish", "waiting")):
        send(redis_client, stream_prefix, make_job(stage, status))
    svc = service(db, redis_client, stream_prefix, spy)
    for _ in range(3):
        svc.step()
    assert spy.seen == [] and pending(redis_client, stream_prefix) == 0


def test_a_clip_that_had_to_wait_is_looked_at_again_on_every_sweep_without_a_new_message(
    db, redis_client, stream_prefix, make_job
):
    spy, clock, job_id = Spy(), Clock(), make_job()
    svc = service(db, redis_client, stream_prefix, spy, clock)
    svc.step()  # the first sweep is due at once
    assert spy.seen == [job_id]
    clock.now += 30
    svc.step()
    assert spy.seen == [job_id]  # not due yet
    clock.now += 31
    svc.step()
    assert spy.seen == [job_id, job_id]


def test_a_clip_that_fails_unexpectedly_does_not_stop_the_service_or_lose_the_message(
    db, redis_client, stream_prefix, make_job
):
    spy = Spy(RuntimeError("bug"))
    alerts: list[str] = []
    first, second = make_job(), make_job()
    send(redis_client, stream_prefix, first)
    send(redis_client, stream_prefix, second)
    svc = service(db, redis_client, stream_prefix, spy, alert=alerts.append)
    svc.step()
    svc.step()
    assert pending(redis_client, stream_prefix) == 0  # acknowledged: the sweep will try again
    assert set(spy.seen) == {first, second} and any("bug" in a for a in alerts)


def test_the_same_alert_is_not_repeated_within_half_an_hour():
    clock = Clock()
    sent: list[str] = []
    throttled = Throttle(sent.append, clock=clock)
    throttled("sign in again")
    throttled("sign in again")
    throttled("something else")
    clock.now += 1801
    throttled("sign in again")
    assert sent == ["sign in again", "something else", "sign in again"]
