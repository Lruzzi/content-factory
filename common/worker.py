"""The service contract as code (CLAUDE.md, TECH.md: Job handling and recovery).

1. read a job id; 2. load the job: if it already moved on, re-publish it instead of working again;
3. work; 4. write the result and advance the job in Postgres, then publish the next stage, then acknowledge;
5. after `max_attempts` failures, dead-letter the job and alert.

A crash between "advance" and "publish" is safe: the message is redelivered, finds the job already moved on,
and republishes it. The handler runs outside any database transaction, so a long job holds no lock.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import redis
from sqlalchemy import Engine

from common import jobs, pipeline
from common.streams import DEFAULT_MAXLEN, Consumer, Delivery, publish

Handler = Callable[[jobs.Job], str | None]  # returns the next stage, or None when the job is finished


class Worker:
    def __init__(
        self,
        *,
        engine: Engine,
        r: redis.Redis,
        stage: str,
        consumer: Consumer,
        handler: Handler,
        retry: jobs.Retry,
        alert: Callable[[str], object] | None = None,
        prefix: str = "",
        maxlen: int = DEFAULT_MAXLEN,
    ) -> None:
        self.engine, self.r, self.stage = engine, r, pipeline.check_stage(stage)
        self.consumer, self.handler, self.retry = consumer, handler, retry
        self.alert, self.prefix, self.maxlen = alert, prefix, maxlen

    def run_once(self, block_ms: int | None = None) -> bool:
        """Handle at most one message. False when there was none."""
        delivery = self.consumer.read(block_ms)
        if delivery is None:
            return False
        job_id = uuid.UUID(delivery.job_id)
        with self.engine.begin() as conn:
            job = jobs.get(conn, job_id)
            if job is None:
                self.consumer.dead_letter(delivery, "no such job")
                return True
            if job["stage"] != self.stage or job["status"] in ("done", "dead"):
                self._moved_on(delivery, job)
                return True
            if not jobs.claim(conn, job_id):  # another worker is running it: drop this duplicate message
                self.consumer.ack(delivery)
                return True
            job = jobs.get(conn, job_id) or job
        self._work(delivery, job)
        return True

    def _moved_on(self, delivery: Delivery, job: jobs.Job) -> None:
        """The job is past this stage. If it waits in a later queue, publish it there again (the first publish
        may be what was lost); duplicates are harmless because the next stage checks the job first."""
        if job["stage"] != self.stage and job["status"] == "queued":
            publish(self.r, pipeline.stream_for(job["stage"], self.prefix), str(job["id"]), maxlen=self.maxlen)
        self.consumer.ack(delivery)

    def _work(self, delivery: Delivery, job: jobs.Job) -> None:
        job_id = job["id"]
        try:
            next_stage = self.handler(job)
            with self.engine.begin() as conn:
                if next_stage == jobs.WAIT:
                    jobs.hold(conn, job_id)
                    next_stage = None
                else:
                    jobs.advance(conn, job_id, next_stage)
        except jobs.Deferred as later:  # not a failure: the job waits and keeps its attempts
            with self.engine.begin() as conn:
                jobs.defer(conn, job_id, later.wait_s, later.reason)
            self.consumer.ack(delivery)
            return
        except Exception as exc:  # any failure of the work is recorded and retried, never lets the worker die
            self._fail(delivery, job_id, f"{type(exc).__name__}: {exc}")
            return
        if next_stage is not None:
            publish(self.r, pipeline.stream_for(next_stage, self.prefix), str(job_id), maxlen=self.maxlen)
        self.consumer.ack(delivery)

    def _fail(self, delivery: Delivery, job_id: uuid.UUID, error: str) -> None:
        with self.engine.begin() as conn:
            outcome = jobs.record_failure(conn, job_id, error, self.retry)
        if outcome == "dead":
            self.consumer.dead_letter(delivery, f"failed {self.retry.max_attempts} times: {error}")
            if self.alert:
                self.alert(f"job {job_id} is dead at stage {self.stage}: {error}")
        else:  # the reconciler republishes it when its retry is due
            self.consumer.ack(delivery)
