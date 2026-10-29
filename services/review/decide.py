"""The only place a clip becomes approved. Each change is one UPDATE that only matches a job still waiting at the review
stage, so a double tap, a late tap or a tap after expiry cannot decide twice. Who is allowed to tap is checked by the
caller (listener.py), before this is reached."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

DECISIONS = ("approved", "rejected")
DEFAULT_EXPIRE_H = 24


@dataclass(frozen=True)
class Expired:
    job_id: uuid.UUID
    chats: list[dict[str, Any]]
    title: str


def decide(engine: sa.Engine, job_id: uuid.UUID, decision: str, *, by: int) -> bool:
    """Approve (the clip goes on to publishing) or reject (the job is finished). True only for the first decision."""
    if decision not in DECISIONS:
        raise ValueError(f"unknown decision {decision!r}")
    with engine.begin() as conn:
        won = conn.execute(
            sa.text(
                "UPDATE jobs SET "
                "stage = CASE WHEN :d = 'approved' THEN 'publish' ELSE stage END, "
                "status = CASE WHEN :d = 'approved' THEN 'queued' ELSE 'done' END, "
                "attempt = 0, retry_at = NULL, last_error = NULL, "
                "data = jsonb_set(data, '{review}', COALESCE(data->'review', '{}'::jsonb) || CAST(:r AS jsonb)), "
                "updated_at = clock_timestamp() "
                "WHERE id = :id AND stage = 'review' AND status = 'waiting' RETURNING id"
            ),
            {"d": decision, "id": job_id, "r": f'{{"state": "{decision}", "by": {int(by)}}}'},
        ).first()
    return won is not None


def record_reason(engine: sa.Engine, job_id: uuid.UUID, reason: str) -> bool:
    """Keep why a clip was rejected. Only a rejected clip without a reason yet takes one, so the first tap stays."""
    with engine.begin() as conn:
        done = conn.execute(
            sa.text(
                "UPDATE jobs SET data = jsonb_set(data, '{review,reason}', to_jsonb(CAST(:r AS text))) "
                "WHERE id = :id AND data->'review'->>'state' = 'rejected' AND NOT (data->'review' ? 'reason') "
                "RETURNING id"
            ),
            {"r": reason, "id": job_id},
        ).first()
    return done is not None


def review_of(engine: sa.Engine, job_id: uuid.UUID) -> dict[str, Any]:
    """What was stored when the preview was sent: the title and where the messages are."""
    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT data->'review' FROM jobs WHERE id = :i"), {"i": job_id}).scalar_one_or_none()
    return row or {}


def expire_due(engine: sa.Engine) -> list[Expired]:
    """Finish the clips nobody decided on within their profile's `review.expire_after_h`. Returns them, so their
    Telegram messages can be updated. Hours come from each job's own profile snapshot."""
    with engine.begin() as conn:
        rows = conn.execute(
            sa.text(
                "UPDATE jobs SET status = 'done', "
                "data = jsonb_set(data, '{review}', "
                "COALESCE(data->'review', '{}'::jsonb) || '{\"state\": \"expired\"}'::jsonb), "
                "updated_at = clock_timestamp() "
                "WHERE stage = 'review' AND status = 'waiting' AND updated_at < clock_timestamp() - "
                "make_interval(hours => COALESCE((spec->'review'->>'expire_after_h')::int, :default)) "
                "RETURNING id, data->'review'"
            ),
            {"default": DEFAULT_EXPIRE_H},
        ).all()
    return [Expired(r[0], (r[1] or {}).get("chats", []), (r[1] or {}).get("title", "")) for r in rows]
