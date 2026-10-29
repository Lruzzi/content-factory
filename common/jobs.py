"""Jobs: one row per unit of work, moving through the stages (TECH.md: Data and messaging).

A job stores `spec`, the resolved profile taken when it was created, so services never read the YAML
or the sources table for settings and an edit mid-run cannot change a job in flight (NFR-12).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

import config_schema as cs
from common.pipeline import check_stage

MAX_ERROR_CHARS = 2_000

Job = dict[str, Any]

# A handler returns this instead of a stage to hold the job where it is until a person decides (the Telegram review).
WAIT = "\0wait"


@dataclass(frozen=True)
class Retry:
    """`max_attempts` failures kill a job; `backoff_s[n]` is the wait after failure n+1 (the last repeats)."""

    max_attempts: int = 3
    backoff_s: Sequence[float] = (60, 300, 1800)

    def delay_after(self, attempt: int) -> float:
        return self.backoff_s[min(attempt - 1, len(self.backoff_s) - 1)]


def create_job(
    conn: sa.Connection,
    profile: cs.Profile,
    *,
    stage: str,
    source_id: uuid.UUID | None = None,
    media_key: str | None = None,
    kind: str | None = None,
    data: dict[str, Any] | None = None,
) -> uuid.UUID:
    check_stage(stage)
    return conn.execute(
        sa.text(
            "INSERT INTO jobs (source_id, stage, kind, spec, media_key, data) "
            "VALUES (:source_id, :stage, :kind, CAST(:spec AS jsonb), :media_key, CAST(:data AS jsonb)) RETURNING id"
        ),
        {
            "source_id": source_id,
            "stage": stage,
            "kind": kind,
            "spec": json.dumps(profile.model_dump(mode="json")),
            "media_key": media_key,
            "data": json.dumps(data or {}),
        },
    ).scalar_one()


def set_result(
    conn: sa.Connection, job_id: uuid.UUID, *, media_key: str | None = None, data: dict[str, Any] | None = None
) -> None:
    """Store what a stage produced: the new media key, and keys added to (or replaced in) `data`.
    Keys the stage does not mention are kept, so each stage only touches its own."""
    conn.execute(
        sa.text(
            "UPDATE jobs SET media_key = COALESCE(:media_key, media_key), data = data || CAST(:data AS jsonb), "
            "updated_at = clock_timestamp() WHERE id = :id"
        ),
        {"id": job_id, "media_key": media_key, "data": json.dumps(data or {})},
    )


def get(conn: sa.Connection, job_id: uuid.UUID) -> Job | None:
    row = conn.execute(sa.text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id}).mappings().first()
    return dict(row) if row else None


def claim(conn: sa.Connection, job_id: uuid.UUID) -> bool:
    """Mark a queued or failed job as running. False when another worker has it, or it is done or dead."""
    row = conn.execute(
        sa.text(
            "UPDATE jobs SET status = 'running', updated_at = clock_timestamp() "
            "WHERE id = :id AND status IN ('queued', 'failed') RETURNING id"
        ),
        {"id": job_id},
    ).first()
    return row is not None


def hold(conn: sa.Connection, job_id: uuid.UUID) -> None:
    """Park a running job at its stage until a person decides. No worker, retry or sweep touches a waiting job."""
    conn.execute(
        sa.text("UPDATE jobs SET status = 'waiting', updated_at = clock_timestamp() WHERE id = :id"), {"id": job_id}
    )


def advance(conn: sa.Connection, job_id: uuid.UUID, next_stage: str | None) -> None:
    """Move a job to the next stage, queued with fresh attempts, or finish it when there is none."""
    if next_stage is None:
        conn.execute(
            sa.text("UPDATE jobs SET status = 'done', retry_at = NULL, updated_at = clock_timestamp() WHERE id = :id"),
            {"id": job_id},
        )
        return
    conn.execute(
        sa.text(
            "UPDATE jobs SET stage = :stage, status = 'queued', attempt = 0, retry_at = NULL, last_error = NULL, "
            "updated_at = clock_timestamp() WHERE id = :id"
        ),
        {"id": job_id, "stage": check_stage(next_stage)},
    )


def record_failure(conn: sa.Connection, job_id: uuid.UUID, error: str, retry: Retry) -> str:
    """Count a failed attempt. Returns "failed" (retry scheduled after the backoff) or "dead" (out of attempts)."""
    attempt = (
        conn.execute(sa.text("SELECT attempt FROM jobs WHERE id = :id FOR UPDATE"), {"id": job_id}).scalar_one() + 1
    )
    dead = attempt >= retry.max_attempts
    conn.execute(
        sa.text(
            "UPDATE jobs SET attempt = :attempt, last_error = :error, updated_at = clock_timestamp(), "
            "status = CASE WHEN :dead THEN 'dead' ELSE 'failed' END, "
            "retry_at = CASE WHEN :dead THEN NULL "
            "ELSE clock_timestamp() + make_interval(secs => CAST(:delay AS double precision)) END "
            "WHERE id = :id"
        ),
        {
            "id": job_id,
            "attempt": attempt,
            "error": error[:MAX_ERROR_CHARS],
            "dead": dead,
            "delay": 0 if dead else retry.delay_after(attempt),
        },
    )
    return "dead" if dead else "failed"


class Deferred(Exception):
    """Raised by a handler that cannot do its work yet and is not failing (a rate limit, a quota): the job is put back
    for `wait_s` seconds and keeps all its attempts."""

    def __init__(self, wait_s: float, reason: str) -> None:
        super().__init__(reason)
        self.wait_s, self.reason = wait_s, reason


def defer(conn: sa.Connection, job_id: uuid.UUID, wait_s: float, reason: str) -> None:
    """Put a job back for later without counting an attempt. It is marked `failed` with a retry time, so the reconciler
    queues it again when the time has passed, the same path a failed attempt takes."""
    conn.execute(
        sa.text(
            "UPDATE jobs SET status = 'failed', last_error = :reason, updated_at = clock_timestamp(), "
            "retry_at = clock_timestamp() + make_interval(secs => CAST(:wait AS double precision)) WHERE id = :id"
        ),
        {"id": job_id, "reason": reason[:MAX_ERROR_CHARS], "wait": wait_s},
    )


def requeue_dead(conn: sa.Connection, job_id: uuid.UUID) -> bool:
    """Give a dead job a fresh start (the caller publishes it). False if the job is not dead."""
    row = conn.execute(
        sa.text(
            "UPDATE jobs SET status = 'queued', attempt = 0, retry_at = NULL, updated_at = clock_timestamp() "
            "WHERE id = :id AND status = 'dead' RETURNING id"
        ),
        {"id": job_id},
    ).first()
    return row is not None
