"""The reconciler: Postgres is the truth, so it re-publishes what the queue lost (FR-18)."""

from __future__ import annotations

import threading

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import jobs, pipeline, reconciler

pytestmark = pytest.mark.integration

RETRY = jobs.Retry(max_attempts=3, backoff_s=(60, 300, 1800))


def a_profile():
    return next(p for p in cs.load_profile(cs.ROOT / "profiles" / "mlbb.yaml") if p.source.id == "leokabuki")


def make_job(db, stage="understand"):
    with db.begin() as conn:
        return jobs.create_job(conn, a_profile(), stage=stage)


def set_state(db, job_id, **sql):
    """Set columns directly; values are SQL expressions such as "clock_timestamp() - interval '20 minutes'"."""
    assignments = ", ".join(f"{column} = {expression}" for column, expression in sql.items())
    with db.begin() as conn:
        conn.execute(sa.text(f"UPDATE jobs SET {assignments} WHERE id = :i"), {"i": job_id})  # noqa: S608


def sweep(db, redis_client, prefix, alerts=None, **kw):
    options = {
        "stalled_after_min": 10,
        "running_after_min": 60,
        "retry": RETRY,
        "prefix": prefix,
        "dlq": f"{prefix}.dlq",
    }
    return reconciler.sweep(db, redis_client, alert=(alerts.append if alerts is not None else None), **(options | kw))


def length(redis_client, prefix, stage):
    return redis_client.xlen(pipeline.stream_for(stage, prefix))


def row(db, job_id):
    with db.connect() as conn:
        job = jobs.get(conn, job_id)
    assert job is not None
    return job


def status(db, job_id):
    return row(db, job_id)["status"]


def test_a_due_retry_is_requeued_and_published(db, redis_client, stream_prefix):
    job_id = make_job(db)
    set_state(db, job_id, status="'failed'", attempt="1", retry_at="clock_timestamp() - interval '1 second'")
    result = sweep(db, redis_client, stream_prefix)
    assert result.retried == 1
    assert status(db, job_id) == "queued"
    assert length(redis_client, stream_prefix, "understand") == 1


def test_a_retry_that_is_not_due_yet_is_left_alone(db, redis_client, stream_prefix):
    job_id = make_job(db)
    set_state(db, job_id, status="'failed'", attempt="1", retry_at="clock_timestamp() + interval '5 minutes'")
    assert sweep(db, redis_client, stream_prefix).retried == 0
    assert status(db, job_id) == "failed"
    assert length(redis_client, stream_prefix, "understand") == 0


def test_a_queued_job_whose_message_was_lost_is_published_again(db, redis_client, stream_prefix):
    job_id = make_job(db)
    set_state(db, job_id, updated_at="clock_timestamp() - interval '20 minutes'")
    assert sweep(db, redis_client, stream_prefix).republished == 1
    assert length(redis_client, stream_prefix, "understand") == 1
    assert status(db, job_id) == "queued"


def test_a_fresh_queued_job_is_not_published_again(db, redis_client, stream_prefix):
    make_job(db)
    assert sweep(db, redis_client, stream_prefix).republished == 0


def test_a_republished_job_is_not_republished_on_the_very_next_sweep(db, redis_client, stream_prefix):
    job_id = make_job(db)
    set_state(db, job_id, updated_at="clock_timestamp() - interval '20 minutes'")
    sweep(db, redis_client, stream_prefix)
    assert sweep(db, redis_client, stream_prefix).republished == 0
    assert length(redis_client, stream_prefix, "understand") == 1


def test_a_job_stuck_running_for_too_long_counts_as_a_failed_attempt(db, redis_client, stream_prefix):
    job_id = make_job(db)
    set_state(db, job_id, status="'running'", updated_at="clock_timestamp() - interval '90 minutes'")
    result = sweep(db, redis_client, stream_prefix)
    assert result.timed_out == 1
    job = row(db, job_id)
    assert (job["status"], job["attempt"]) == ("failed", 1)
    assert "timed out" in job["last_error"]


def test_a_job_that_has_only_just_started_running_is_left_alone(db, redis_client, stream_prefix):
    job_id = make_job(db)
    set_state(db, job_id, status="'running'")
    assert sweep(db, redis_client, stream_prefix).timed_out == 0
    assert status(db, job_id) == "running"


def test_a_job_that_times_out_for_the_last_time_is_dead_lettered_with_an_alert(db, redis_client, stream_prefix):
    alerts: list[str] = []
    job_id = make_job(db)
    set_state(db, job_id, status="'running'", attempt="2", updated_at="clock_timestamp() - interval '90 minutes'")
    result = sweep(db, redis_client, stream_prefix, alerts=alerts)
    assert result.dead == 1
    assert status(db, job_id) == "dead"
    assert [d[1]["job_id"] for d in redis_client.xrange(f"{stream_prefix}.dlq")] == [str(job_id)]
    assert len(alerts) == 1


def test_done_and_dead_jobs_are_never_touched(db, redis_client, stream_prefix):
    done, dead = make_job(db), make_job(db)
    set_state(db, done, status="'done'", updated_at="clock_timestamp() - interval '2 days'")
    set_state(db, dead, status="'dead'", updated_at="clock_timestamp() - interval '2 days'")
    result = sweep(db, redis_client, stream_prefix)
    assert (result.retried, result.republished, result.timed_out) == (0, 0, 0)


def test_two_sweeps_at_once_do_not_both_run(db, redis_client, stream_prefix):
    job_id = make_job(db)
    set_state(db, job_id, updated_at="clock_timestamp() - interval '20 minutes'")
    holder_ready, release = threading.Event(), threading.Event()

    def hold_the_lock():
        with db.begin() as conn:
            conn.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": reconciler.LOCK_KEY})
            holder_ready.set()
            release.wait(10)

    thread = threading.Thread(target=hold_the_lock)
    thread.start()
    assert holder_ready.wait(10)
    try:
        assert sweep(db, redis_client, stream_prefix) is None  # another sweep holds the lock: skip, do not wait
    finally:
        release.set()
        thread.join()
    assert length(redis_client, stream_prefix, "understand") == 0
    assert sweep(db, redis_client, stream_prefix).republished == 1


def test_streams_for_each_stage_are_used(db, redis_client, stream_prefix):
    job_id = make_job(db, stage="render")
    set_state(db, job_id, updated_at="clock_timestamp() - interval '20 minutes'")
    sweep(db, redis_client, stream_prefix)
    assert length(redis_client, stream_prefix, "render") == 1
    assert length(redis_client, stream_prefix, "understand") == 0


def test_a_job_waiting_for_a_person_is_left_alone_however_old_it_is(db, redis_client, stream_prefix):
    job_id = make_job(db, stage="review")
    set_state(db, job_id, status="'waiting'", updated_at="clock_timestamp() - interval '3 days'")
    sweep(db, redis_client, stream_prefix)
    assert row(db, job_id)["status"] == "waiting" and length(redis_client, stream_prefix, "review") == 0
