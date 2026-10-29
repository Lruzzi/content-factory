"""Files kept in R2 for a while: what is stored, what you asked to delete, what has expired, what is gone. The review
service only records that you asked (it holds no storage keys); the TikTok publisher, which does, deletes."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa


@dataclass(frozen=True)
class HostedFile:
    id: uuid.UUID
    post_id: uuid.UUID | None
    key: str
    bytes: int
    chats: list[dict[str, Any]]


def _file(row: Any) -> HostedFile:
    return HostedFile(row[0], row[1], row[2], int(row[3]), list(row[4]))


def add(
    conn: sa.Connection,
    post_id: uuid.UUID | None,
    key: str,
    size: int,
    chats: list[dict[str, Any]],
    *,
    keep_h: float | None = None,
) -> uuid.UUID:
    """Remember a stored file. The same key twice is one file (the later chats win, and a `keep_h` given before stays
    unless a new one is given). `keep_h` is how many hours from now it is due for deletion; without one, by age."""
    return conn.execute(
        sa.text(
            "INSERT INTO hosted_files (post_id, key, bytes, chats, keep_until) VALUES (:p, :k, :b, CAST(:c AS jsonb), "
            "CASE WHEN CAST(:h AS double precision) IS NULL THEN NULL "
            "ELSE clock_timestamp() + make_interval(secs => CAST(:h AS double precision) * 3600) END) "
            "ON CONFLICT (tenant_id, key) DO UPDATE SET bytes = EXCLUDED.bytes, chats = EXCLUDED.chats, "
            "post_id = COALESCE(EXCLUDED.post_id, hosted_files.post_id), "
            "keep_until = COALESCE(EXCLUDED.keep_until, hosted_files.keep_until), "
            "deleted_at = NULL, delete_requested_at = NULL RETURNING id"
        ),
        {"p": post_id, "k": key, "b": size, "c": json.dumps(chats), "h": keep_h},
    ).scalar_one()


def set_keep(conn: sa.Connection, key: str, keep_h: float) -> bool:
    """Make a stored file due for deletion `keep_h` hours from now. False when it is not stored (any more)."""
    return (
        conn.execute(
            sa.text(
                "UPDATE hosted_files SET keep_until = clock_timestamp() + "
                "make_interval(secs => CAST(:h AS double precision) * 3600) "
                "WHERE key = :k AND deleted_at IS NULL RETURNING id"
            ),
            {"k": key, "h": keep_h},
        ).first()
        is not None
    )


def live(conn: sa.Connection, key: str) -> HostedFile | None:
    """The record of a file that is stored (not deleted), found by its key."""
    row = conn.execute(
        sa.text("SELECT id, post_id, key, bytes, chats FROM hosted_files WHERE key = :k AND deleted_at IS NULL"),
        {"k": key},
    ).first()
    return None if row is None else _file(row)


def get(conn: sa.Connection, file_id: uuid.UUID | str) -> HostedFile | None:
    row = conn.execute(
        sa.text("SELECT id, post_id, key, bytes, chats FROM hosted_files WHERE id = :i"), {"i": file_id}
    ).first()
    return None if row is None else _file(row)


def total_bytes(conn: sa.Connection) -> int:
    """What is stored right now, to keep under the size cap."""
    return int(
        conn.execute(sa.text("SELECT COALESCE(sum(bytes), 0) FROM hosted_files WHERE deleted_at IS NULL")).scalar_one()
    )


def request_delete(conn: sa.Connection, file_id: uuid.UUID | str) -> bool:
    """You pressed Delete. True only the first time, and only for a file that is still there."""
    return (
        conn.execute(
            sa.text(
                "UPDATE hosted_files SET delete_requested_at = clock_timestamp() "
                "WHERE id = :i AND deleted_at IS NULL AND delete_requested_at IS NULL RETURNING id"
            ),
            {"i": file_id},
        ).first()
        is not None
    )


def due(conn: sa.Connection, *, max_age_h: float) -> list[tuple[HostedFile, str]]:
    """Files to delete now, with why: `asked` (you pressed Delete) or `expired` (their `keep_until` has passed, or they
    are older than `max_age_h`, whatever else is set)."""
    rows = conn.execute(
        sa.text(
            "SELECT id, post_id, key, bytes, chats, delete_requested_at IS NOT NULL FROM hosted_files "
            "WHERE deleted_at IS NULL AND (delete_requested_at IS NOT NULL OR keep_until <= clock_timestamp() "
            "OR created_at < clock_timestamp() - make_interval(secs => CAST(:s AS double precision))) "
            "ORDER BY created_at"
        ),
        {"s": max_age_h * 3600},
    ).all()
    return [(_file(r), "asked" if r[5] else "expired") for r in rows]


def mark_deleted(conn: sa.Connection, file_id: uuid.UUID | str) -> None:
    conn.execute(sa.text("UPDATE hosted_files SET deleted_at = clock_timestamp() WHERE id = :i"), {"i": file_id})


def forget(conn: sa.Connection, key: str) -> None:
    """The file with this key has been deleted by whoever stored it (an Instagram clip, once Instagram has it)."""
    conn.execute(
        sa.text("UPDATE hosted_files SET deleted_at = clock_timestamp() WHERE key = :k AND deleted_at IS NULL"),
        {"k": key},
    )
