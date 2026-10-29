"""The `posts` table: one row per clip per account, written before the platform is called. The row is what makes a
redelivery safe: a crashed upload is found again (with its upload session) instead of started over."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa

Post = dict[str, Any]


def claim(conn: sa.Connection, job_id: uuid.UUID, account_id: str, platform: str) -> tuple[Post, bool]:
    """The post row for this clip and account, created as `uploading` if missing. True when it was just made."""
    made = (
        conn.execute(
            sa.text(
                "INSERT INTO posts (job_id, account_id, platform, idempotency_key) VALUES (:j, :a, :p, :k) "
                "ON CONFLICT (tenant_id, job_id, account_id) DO NOTHING RETURNING *"
            ),
            {"j": job_id, "a": account_id, "p": platform, "k": f"{job_id}:{account_id}"},
        )
        .mappings()
        .first()
    )
    if made is not None:
        return dict(made), True
    row = (
        conn.execute(
            sa.text("SELECT * FROM posts WHERE job_id = :j AND account_id = :a"), {"j": job_id, "a": account_id}
        )
        .mappings()
        .one()
    )
    return dict(row), False


def existing(conn: sa.Connection, job_id: uuid.UUID, account_id: str) -> Post | None:
    row = (
        conn.execute(
            sa.text("SELECT * FROM posts WHERE job_id = :j AND account_id = :a"), {"j": job_id, "a": account_id}
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def record_session(conn: sa.Connection, post_id: uuid.UUID, session_url: str) -> None:
    conn.execute(
        sa.text("UPDATE posts SET session_url = :s, updated_at = clock_timestamp() WHERE id = :i"),
        {"s": session_url, "i": post_id},
    )


def mark_posted(conn: sa.Connection, post_id: uuid.UUID, remote_id: str, remote_url: str) -> None:
    conn.execute(
        sa.text(
            "UPDATE posts SET state = 'posted', remote_id = :r, remote_url = :u, error = NULL, "
            "posted_at = clock_timestamp(), updated_at = clock_timestamp() WHERE id = :i"
        ),
        {"r": remote_id, "u": remote_url, "i": post_id},
    )


def record_failure(conn: sa.Connection, post_id: uuid.UUID, error: str, *, max_attempts: int) -> str:
    """Count a failed attempt. The post stays `uploading` (to be tried again) until `max_attempts`, then `failed`."""
    return conn.execute(
        sa.text(
            "UPDATE posts SET attempts = attempts + 1, error = :e, updated_at = clock_timestamp(), "
            "state = CASE WHEN attempts + 1 >= :m THEN 'failed' ELSE state END WHERE id = :i RETURNING state"
        ),
        {"e": error[:2000], "m": max_attempts, "i": post_id},
    ).scalar_one()


def fail_now(conn: sa.Connection, post_id: uuid.UUID, error: str) -> None:
    """A failure that trying again cannot fix (the daily quota, a refused video)."""
    conn.execute(
        sa.text("UPDATE posts SET state = 'failed', error = :e, updated_at = clock_timestamp() WHERE id = :i"),
        {"e": error[:2000], "i": post_id},
    )


def skip(conn: sa.Connection, job_id: uuid.UUID, account_id: str, platform: str, reason: str) -> None:
    """Record that this clip will not go to this account, and why. A clip already posted is never overwritten."""
    conn.execute(
        sa.text(
            "INSERT INTO posts (job_id, account_id, platform, idempotency_key, state, error) "
            "VALUES (:j, :a, :p, :k, 'skipped', :r) ON CONFLICT (tenant_id, job_id, account_id) DO NOTHING"
        ),
        {"j": job_id, "a": account_id, "p": platform, "k": f"{job_id}:{account_id}", "r": reason},
    )


def recent_times(conn: sa.Connection, account_id: str) -> list[datetime]:
    """When this account's posts were started (uploading or posted): what its daily cap and spacing count."""
    rows = conn.execute(
        sa.text(
            "SELECT created_at FROM posts WHERE account_id = :a AND state IN ('uploading', 'posted') "
            "AND created_at > clock_timestamp() - interval '2 days'"
        ),
        {"a": account_id},
    ).scalars()
    return list(rows)


def all_final(conn: sa.Connection, job_id: uuid.UUID, accounts: list[str]) -> bool:
    """True when every account the clip was meant for has a posted, failed or skipped row."""
    done = conn.execute(
        sa.text(
            "SELECT count(*) FROM posts WHERE job_id = :j AND account_id = ANY(:a) "
            "AND state IN ('posted', 'failed', 'skipped')"
        ),
        {"j": job_id, "a": accounts},
    ).scalar_one()
    return done == len(set(accounts))
