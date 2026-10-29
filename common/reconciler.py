"""The reconciler: Postgres is the source of truth, Redis only carries ids, so what the queue loses is rebuilt here.

One sweep, safe to run twice (an advisory lock makes a second copy skip):
- retries that are due go back to queued and are published;
- jobs stuck "running" past `running_after_min` count as a failed attempt (their worker is gone and its
  message was lost);
- queued jobs untouched for `stalled_after_min` are published again (their message was lost).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

import redis
import sqlalchemy as sa

from common import jobs, pipeline
from common.streams import DEFAULT_DLQ_MAXLEN, DEFAULT_MAXLEN, publish, send_to_dlq

LOCK_KEY = 0x636F6E66  # "conf": the advisory lock the sweep takes, shared by every copy


@dataclass(frozen=True)
class Swept:
    retried: int
    republished: int
    timed_out: int
    dead: int


def sweep(
    engine: sa.Engine,
    r: redis.Redis,
    *,
    stalled_after_min: float,
    running_after_min: float,
    retry: jobs.Retry,
    prefix: str = "",
    dlq: str = "dlq",
    alert: Callable[[str], object] | None = None,
    maxlen: int = DEFAULT_MAXLEN,
    dlq_maxlen: int = DEFAULT_DLQ_MAXLEN,
) -> Swept | None:
    """Run one sweep, or return None when another sweep holds the lock."""
    to_publish: list[tuple[uuid.UUID, str]] = []
    dead: list[tuple[uuid.UUID, str, str]] = []
    with engine.begin() as conn:
        if not conn.execute(sa.text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": LOCK_KEY}).scalar_one():
            return None
        retried = conn.execute(
            sa.text(
                "UPDATE jobs SET status = 'queued', retry_at = NULL, updated_at = clock_timestamp() "
                "WHERE status = 'failed' AND retry_at <= clock_timestamp() RETURNING id, stage"
            )
        ).all()
        stuck = conn.execute(
            sa.text(
                "SELECT id, stage FROM jobs WHERE status = 'running' "
                "AND updated_at < clock_timestamp() - make_interval(secs => CAST(:s AS double precision)) "
                "FOR UPDATE SKIP LOCKED"
            ),
            {"s": running_after_min * 60},
        ).all()
        for job_id, stage in stuck:
            error = f"timed out while running for over {running_after_min:g} min"
            if jobs.record_failure(conn, job_id, error, retry) == "dead":
                dead.append((job_id, stage, error))
        stalled = conn.execute(
            sa.text(
                "UPDATE jobs SET updated_at = clock_timestamp() WHERE status = 'queued' "
                "AND updated_at < clock_timestamp() - make_interval(secs => CAST(:s AS double precision)) "
                "RETURNING id, stage"
            ),
            {"s": stalled_after_min * 60},
        ).all()
    to_publish = [(job_id, stage) for job_id, stage in [*retried, *stalled]]
    for job_id, stage in to_publish:  # after the commit: a crash here is healed by the next sweep
        publish(r, pipeline.stream_for(stage, prefix), str(job_id), maxlen=maxlen)
    for job_id, stage, error in dead:
        send_to_dlq(
            r,
            dlq,
            stream=pipeline.stream_for(stage, prefix),
            group="",
            message_id="",
            job_id=str(job_id),
            reason=error,
            deliveries=retry.max_attempts,
            maxlen=dlq_maxlen,
        )
        if alert:
            alert(f"job {job_id} is dead at stage {stage}: {error}")
    return Swept(retried=len(retried), republished=len(stalled), timed_out=len(stuck), dead=len(dead))
