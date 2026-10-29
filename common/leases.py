"""Leases for long-running workers (recorder, chat-listener).

A worker takes the lease on a live stream, acks its queue message at once and refreshes the heartbeat.
Every comparison uses the Postgres clock (clock_timestamp), never a worker's own, so hosts with drifting
clocks cannot expire each other's leases. The reconciler re-queues streams that need a worker.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

# The columns of each kind of lease on a live stream. Fixed names only: they are put into the statements below.
COLUMNS = {"recorder": ("lease_worker", "lease_heartbeat_at"), "chat": ("chat_worker", "chat_heartbeat_at")}


def acquire(conn: sa.Connection, stream_id: uuid.UUID, worker: str, ttl_s: float, kind: str = "recorder") -> bool:
    """Take the lease if it is free, expired, or already this worker's. True when the worker now holds it."""
    who, beat = COLUMNS[kind]
    row = conn.execute(
        sa.text(
            f"UPDATE live_streams SET {who} = :worker, {beat} = clock_timestamp() "  # noqa: S608  # nosec B608
            f"WHERE id = :id AND ({who} IS NULL OR {who} = :worker "
            f"OR {beat} < clock_timestamp() - make_interval(secs => CAST(:ttl AS double precision))) "
            "RETURNING id"
        ),
        {"worker": worker, "id": stream_id, "ttl": ttl_s},
    ).first()
    return row is not None


def heartbeat(conn: sa.Connection, stream_id: uuid.UUID, worker: str, kind: str = "recorder") -> bool:
    """Refresh the lease. False means the worker no longer holds it and must stop recording."""
    who, beat = COLUMNS[kind]
    row = conn.execute(
        sa.text(
            f"UPDATE live_streams SET {beat} = clock_timestamp() "  # noqa: S608  # nosec B608
            f"WHERE id = :id AND {who} = :worker RETURNING id"
        ),
        {"id": stream_id, "worker": worker},
    ).first()
    return row is not None


def release(conn: sa.Connection, stream_id: uuid.UUID, worker: str, kind: str = "recorder") -> None:
    who, beat = COLUMNS[kind]
    conn.execute(
        sa.text(
            f"UPDATE live_streams SET {who} = NULL, {beat} = NULL "  # noqa: S608  # nosec B608
            f"WHERE id = :id AND {who} = :worker"
        ),
        {"id": stream_id, "worker": worker},
    )


def needing_a_worker(conn: sa.Connection, ttl_s: float, kind: str = "recorder") -> list[uuid.UUID]:
    """Live streams nobody is recording: no lease, or one that has gone stale."""
    who, beat = COLUMNS[kind]
    rows = conn.execute(
        sa.text(
            f"SELECT id FROM live_streams WHERE status = 'live' AND ({who} IS NULL "  # noqa: S608  # nosec B608
            f"OR {beat} < clock_timestamp() - make_interval(secs => CAST(:ttl AS double precision)))"
        ),
        {"ttl": ttl_s},
    ).scalars()
    return list(rows)
