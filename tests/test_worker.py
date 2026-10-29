"""The service contract, as code: read, skip-or-republish, work, advance, publish, ack (CLAUDE.md)."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import jobs, pipeline
from common.streams import Consumer, publish
from common.worker import Worker

pytestmark = pytest.mark.integration

RETRY = jobs.Retry(max_attempts=3, backoff_s=(60, 300, 1800))


class Spy:
    """A handler that records its calls and can be told to fail or to pick the next stage."""

    def __init__(self, result="select", error=None):
        self.result, self.error, self.calls = result, error, []

    def __call__(self, job):
        self.calls.append(job["id"])
        if self.error:
            raise self.error
        return self.result


def a_profile():
    return next(p for p in cs.load_profile(cs.ROOT / "profiles" / "mlbb.yaml") if p.source.id == "leokabuki")


def setup(db, redis_client, prefix, handler, stage="understand", alerts=None):
    consumer = Consumer(
        redis_client,
        pipeline.stream_for(stage, prefix),
        "workers",
        "w1",
        visibility_timeout_s=30,
        dlq=f"{prefix}.dlq",
    )
    worker = Worker(
        engine=db,
        r=redis_client,
        stage=stage,
        consumer=consumer,
        handler=handler,
        retry=RETRY,
        alert=(alerts.append if alerts is not None else None),
        prefix=prefix,
    )
    return worker


def queue_job(db, redis_client, prefix, stage="understand"):
    with db.begin() as conn:
        job_id = jobs.create_job(conn, a_profile(), stage=stage)
    publish(redis_client, pipeline.stream_for(stage, prefix), str(job_id))
    return job_id


def state(db, job_id):
    with db.connect() as conn:
        return jobs.get(conn, job_id)


def pending(redis_client, prefix, stage):
    return redis_client.xpending(pipeline.stream_for(stage, prefix), "workers")["pending"]


def length(redis_client, prefix, stage):
    return redis_client.xlen(pipeline.stream_for(stage, prefix))


def test_an_empty_queue_does_nothing(db, redis_client, stream_prefix):
    spy = Spy()
    assert not setup(db, redis_client, stream_prefix, spy).run_once()
    assert spy.calls == []


def test_a_job_is_worked_advanced_published_and_acknowledged(db, redis_client, stream_prefix):
    spy = Spy(result="select")
    job_id = queue_job(db, redis_client, stream_prefix)
    assert setup(db, redis_client, stream_prefix, spy).run_once()
    assert spy.calls == [job_id]
    job = state(db, job_id)
    assert (job["stage"], job["status"]) == ("select", "queued")
    assert length(redis_client, stream_prefix, "select") == 1
    assert pending(redis_client, stream_prefix, "understand") == 0


def test_the_handler_gets_the_whole_job_including_its_spec(db, redis_client, stream_prefix):
    seen: list[dict] = []

    def remember(job):
        seen.append(job)
        return "select"

    job_id = queue_job(db, redis_client, stream_prefix)
    setup(db, redis_client, stream_prefix, remember).run_once()
    assert seen[0]["id"] == job_id
    assert seen[0]["spec"]["source"]["id"] == "leokabuki"


def test_a_handler_that_returns_nothing_finishes_the_job(db, redis_client, stream_prefix):
    job_id = queue_job(db, redis_client, stream_prefix, stage="publish")
    setup(db, redis_client, stream_prefix, Spy(result=None), stage="publish").run_once()
    assert state(db, job_id)["status"] == "done"


def test_a_first_failure_schedules_a_retry_and_acknowledges_the_message(db, redis_client, stream_prefix):
    job_id = queue_job(db, redis_client, stream_prefix)
    setup(db, redis_client, stream_prefix, Spy(error=RuntimeError("boom"))).run_once()
    job = state(db, job_id)
    assert (job["status"], job["attempt"], job["last_error"]) == ("failed", 1, "RuntimeError: boom")
    assert job["retry_at"] is not None
    assert pending(redis_client, stream_prefix, "understand") == 0
    assert length(redis_client, stream_prefix, "select") == 0


def test_the_error_text_names_the_exception_type_for_debugging(db, redis_client, stream_prefix):
    job_id = queue_job(db, redis_client, stream_prefix)
    setup(db, redis_client, stream_prefix, Spy(error=KeyError("missing"))).run_once()
    assert state(db, job_id)["last_error"].startswith("KeyError:")


def test_the_third_failure_dead_letters_the_job_and_alerts_once(db, redis_client, stream_prefix):
    alerts: list[str] = []
    job_id = queue_job(db, redis_client, stream_prefix)
    spy = Spy(error=RuntimeError("boom"))
    for _ in range(3):
        setup(db, redis_client, stream_prefix, spy, alerts=alerts).run_once()
        # the reconciler would re-publish a due retry; here the test publishes the same message again
        publish(redis_client, pipeline.stream_for("understand", stream_prefix), str(job_id))
        with db.begin() as conn:
            conn.execute(sa.text("UPDATE jobs SET retry_at = clock_timestamp() WHERE status = 'failed'"))
    # the loop published once too often after the last failure: drain it, the dead job is not worked again
    setup(db, redis_client, stream_prefix, spy, alerts=alerts).run_once()
    assert state(db, job_id)["status"] == "dead"
    assert len(spy.calls) == 3
    assert len(alerts) == 1
    assert str(job_id) in alerts[0]
    dead = redis_client.xrange(f"{stream_prefix}.dlq")
    assert [d[1]["job_id"] for d in dead] == [str(job_id)]


def test_a_redelivered_message_for_finished_work_republishes_instead_of_working_again(db, redis_client, stream_prefix):
    # the worker saved its result and advanced the job, then crashed before publishing: the old message returns
    job_id = queue_job(db, redis_client, stream_prefix)
    with db.begin() as conn:
        jobs.claim(conn, job_id)
        jobs.advance(conn, job_id, "select")
    spy = Spy()
    setup(db, redis_client, stream_prefix, spy).run_once()
    assert spy.calls == []
    assert length(redis_client, stream_prefix, "select") == 1
    assert pending(redis_client, stream_prefix, "understand") == 0


def test_a_message_for_a_job_somebody_else_is_running_is_dropped_not_worked_twice(db, redis_client, stream_prefix):
    job_id = queue_job(db, redis_client, stream_prefix)
    with db.begin() as conn:
        jobs.claim(conn, job_id)  # another worker is on it
    spy = Spy()
    setup(db, redis_client, stream_prefix, spy).run_once()
    assert spy.calls == []
    assert state(db, job_id)["status"] == "running"
    assert pending(redis_client, stream_prefix, "understand") == 0


def test_a_message_for_a_finished_job_is_just_acknowledged(db, redis_client, stream_prefix):
    job_id = queue_job(db, redis_client, stream_prefix, stage="publish")
    with db.begin() as conn:
        jobs.claim(conn, job_id)
        jobs.advance(conn, job_id, None)
    spy = Spy()
    setup(db, redis_client, stream_prefix, spy, stage="publish").run_once()
    assert spy.calls == []
    assert pending(redis_client, stream_prefix, "publish") == 0


def test_a_message_for_a_job_that_does_not_exist_is_dead_lettered(db, redis_client, stream_prefix):
    publish(redis_client, pipeline.stream_for("understand", stream_prefix), str(uuid.uuid4()))
    spy = Spy()
    setup(db, redis_client, stream_prefix, spy).run_once()
    assert spy.calls == []
    dead = redis_client.xrange(f"{stream_prefix}.dlq")
    assert len(dead) == 1
    assert "no such job" in dead[0][1]["reason"]


def test_a_handler_cannot_send_a_job_to_a_stage_that_does_not_exist(db, redis_client, stream_prefix):
    job_id = queue_job(db, redis_client, stream_prefix)
    setup(db, redis_client, stream_prefix, Spy(result="bogus")).run_once()
    job = state(db, job_id)
    assert job["status"] == "failed"
    assert "unknown stage" in job["last_error"]


def test_a_handler_can_hold_a_job_for_a_person_without_moving_it_or_queueing_it(db, redis_client, stream_prefix):
    job_id = queue_job(db, redis_client, stream_prefix, stage="review")
    setup(db, redis_client, stream_prefix, lambda job: jobs.WAIT, stage="review").run_once()
    job = state(db, job_id)
    assert (job["stage"], job["status"], job["attempt"]) == ("review", "waiting", 0)
    assert pending(redis_client, stream_prefix, "review") == 0  # acknowledged
    assert length(redis_client, stream_prefix, "publish") == 0  # nothing goes on until someone decides


def test_a_waiting_job_is_not_a_delivery_target_so_a_repeat_message_is_dropped(db, redis_client, stream_prefix):
    job_id = queue_job(db, redis_client, stream_prefix, stage="review")
    setup(db, redis_client, stream_prefix, lambda job: jobs.WAIT, stage="review").run_once()
    publish(redis_client, pipeline.stream_for("review", stream_prefix), str(job_id))  # a duplicate message
    spy = Spy()
    setup(db, redis_client, stream_prefix, spy, stage="review").run_once()
    assert spy.calls == [] and state(db, job_id)["status"] == "waiting"


def test_a_handler_that_says_not_now_defers_the_job_without_a_failure_alert_or_a_lost_attempt(
    db, redis_client, stream_prefix
):
    def not_now(job):
        raise jobs.Deferred(120, "waiting for the Groq quota")

    alerts: list[str] = []
    job_id = queue_job(db, redis_client, stream_prefix)
    setup(db, redis_client, stream_prefix, not_now, alerts=alerts).run_once()
    job = state(db, job_id)
    assert (job["status"], job["attempt"]) == ("failed", 0) and "Groq quota" in job["last_error"]
    assert job["retry_at"] is not None and pending(redis_client, stream_prefix, "understand") == 0
    assert alerts == [] and redis_client.xlen(f"{stream_prefix}.dlq") == 0
