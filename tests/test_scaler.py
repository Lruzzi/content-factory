"""The scaler loop runs the reconciler on a timer, so lost queue messages and due retries heal by themselves."""

from __future__ import annotations

import threading
import time

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import jobs, pipeline, sources
from services.scaler import loop

pytestmark = pytest.mark.integration

RETRY = jobs.Retry(max_attempts=3, backoff_s=(60, 300, 1800))


@pytest.fixture
def make_job(db):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
    profile = next(p for p in profiles if p.source.id == "leokabuki")

    def make(stage="understand", **columns):
        with db.begin() as conn:
            job_id = jobs.create_job(conn, profile, stage=stage)
            for column, expression in columns.items():
                conn.execute(sa.text(f"UPDATE jobs SET {column} = {expression} WHERE id = :i"), {"i": job_id})  # noqa: S608
            return job_id

    return make


def run_loop(db, redis_client, prefix, alerts=None, sweep=None, every_s=0.05):
    stop = threading.Event()
    options = {
        "every_s": every_s, "stalled_after_min": 10, "running_after_min": 60, "retry": RETRY, "prefix": prefix,
        "dlq": f"{prefix}.dlq", "alert": (alerts.append if alerts is not None else None), "stop": stop,
    }  # fmt: skip
    if sweep is not None:
        options["sweep"] = sweep
    thread = threading.Thread(target=loop.run, args=(db, redis_client), kwargs=options, daemon=True)
    thread.start()
    return stop, thread


def wait_for(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_a_queued_job_whose_message_was_lost_is_published_again(db, redis_client, stream_prefix, make_job):
    make_job(updated_at="clock_timestamp() - interval '20 minutes'")
    stop, thread = run_loop(db, redis_client, stream_prefix)
    try:
        assert wait_for(lambda: redis_client.xlen(pipeline.stream_for("understand", stream_prefix)) >= 1)
    finally:
        stop.set()
        thread.join(5)


def test_a_retry_that_has_come_due_is_queued_and_published(db, redis_client, stream_prefix, make_job):
    job_id = make_job(status="'failed'", retry_at="clock_timestamp() - interval '1 minute'")
    stop, thread = run_loop(db, redis_client, stream_prefix)
    try:
        assert wait_for(lambda: redis_client.xlen(pipeline.stream_for("understand", stream_prefix)) >= 1)
        with db.connect() as conn:
            job = jobs.get(conn, job_id)
            assert job is not None and job["status"] == "queued"
    finally:
        stop.set()
        thread.join(5)


def test_the_loop_stops_promptly_when_asked(db, redis_client, stream_prefix):
    stop, thread = run_loop(db, redis_client, stream_prefix, every_s=30)
    time.sleep(0.2)
    started = time.monotonic()
    stop.set()
    thread.join(5)
    assert not thread.is_alive() and time.monotonic() - started < 2


def test_a_sweep_that_fails_is_reported_and_the_loop_carries_on(db, redis_client, stream_prefix):
    calls: list[int] = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("redis went away")

    alerts: list[str] = []
    stop, thread = run_loop(db, redis_client, stream_prefix, alerts=alerts, sweep=flaky)
    try:
        assert wait_for(lambda: len(calls) >= 3)  # it kept running after the failure
    finally:
        stop.set()
        thread.join(5)
    assert len(alerts) == 1 and "redis went away" in alerts[0]


def test_two_loops_running_together_do_not_publish_a_job_twice(db, redis_client, stream_prefix, make_job):
    make_job(updated_at="clock_timestamp() - interval '20 minutes'")
    first = run_loop(db, redis_client, stream_prefix)
    second = run_loop(db, redis_client, stream_prefix)
    try:
        assert wait_for(lambda: redis_client.xlen(pipeline.stream_for("understand", stream_prefix)) >= 1)
        time.sleep(0.4)
        assert redis_client.xlen(pipeline.stream_for("understand", stream_prefix)) == 1
    finally:
        for stop, thread in (first, second):
            stop.set()
            thread.join(5)
