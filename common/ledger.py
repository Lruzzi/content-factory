"""The cost ledger: every paid call is recorded, and a profile stops at its daily budget (FR-13).

Days follow the operator's timezone (accounts.yaml `timezone`), not the server's.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import sqlalchemy as sa


def record_cost(
    conn: sa.Connection,
    *,
    profile: str,
    service: str,
    provider: str,
    units: Decimal | int | float,
    unit: str,
    usd: Decimal,
    job_id: uuid.UUID | None = None,
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO costs (job_id, profile, service, provider, units, unit, usd) "
            "VALUES (:job_id, :profile, :service, :provider, :units, :unit, :usd)"
        ),
        {
            "job_id": job_id,
            "profile": profile,
            "service": service,
            "provider": provider,
            "units": units,
            "unit": unit,
            "usd": usd,
        },
    )


def spent_on(conn: sa.Connection, profile: str, day: date, tz: str) -> Decimal:
    return conn.execute(
        sa.text(
            "SELECT COALESCE(SUM(usd), 0) FROM costs "
            "WHERE profile = :profile AND (created_at AT TIME ZONE :tz)::date = :day"
        ),
        {"profile": profile, "day": day, "tz": tz},
    ).scalar_one()


def spent_today(conn: sa.Connection, profile: str, tz: str) -> Decimal:
    return conn.execute(
        sa.text(
            "SELECT COALESCE(SUM(usd), 0) FROM costs WHERE profile = :profile "
            "AND (created_at AT TIME ZONE :tz)::date = (clock_timestamp() AT TIME ZONE :tz)::date"
        ),
        {"profile": profile, "tz": tz},
    ).scalar_one()


def over_budget(conn: sa.Connection, profile: str, limit_usd: Decimal, tz: str) -> bool:
    """True once today's spend has reached the limit: the profile must pause."""
    return spent_today(conn, profile, tz) >= limit_usd


def units_today(conn: sa.Connection, service: str, unit: str, tz: str) -> Decimal:
    """Units used today across all profiles, such as audio seconds sent to Groq (FR-17)."""
    return conn.execute(
        sa.text(
            "SELECT COALESCE(SUM(units), 0) FROM costs WHERE service = :service AND unit = :unit "
            "AND (created_at AT TIME ZONE :tz)::date = (clock_timestamp() AT TIME ZONE :tz)::date"
        ),
        {"service": service, "unit": unit, "tz": tz},
    ).scalar_one()
