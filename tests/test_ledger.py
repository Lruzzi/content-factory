"""Cost ledger and the daily budget stop (FR-13)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from db_helpers import CHECK, violation

from common import ledger

pytestmark = pytest.mark.integration

JAKARTA = "Asia/Jakarta"


def spend(conn, usd, *, profile="mlbb", service="select", units=1000, unit="tokens"):
    ledger.record_cost(conn, profile=profile, service=service, provider="anthropic", units=units, unit=unit, usd=usd)


def backdate(db, hours):
    with db.begin() as conn:
        conn.execute(
            sa.text("UPDATE costs SET created_at = clock_timestamp() - make_interval(hours => :h)"), {"h": hours}
        )


def test_recorded_costs_add_up_for_today(db):
    with db.begin() as conn:
        spend(conn, Decimal("0.50"))
        spend(conn, Decimal("0.25"))
    with db.connect() as conn:
        assert ledger.spent_today(conn, "mlbb", JAKARTA) == Decimal("0.75")


def test_other_profiles_are_not_counted(db):
    with db.begin() as conn:
        spend(conn, Decimal("0.50"))
        spend(conn, Decimal("9.00"), profile="podcast_id")
    with db.connect() as conn:
        assert ledger.spent_today(conn, "mlbb", JAKARTA) == Decimal("0.50")


def test_nothing_spent_is_zero(db):
    with db.connect() as conn:
        assert ledger.spent_today(conn, "mlbb", JAKARTA) == Decimal(0)


def test_yesterdays_costs_do_not_count_today(db):
    with db.begin() as conn:
        spend(conn, Decimal("1.00"))
    backdate(db, 48)
    with db.connect() as conn:
        assert ledger.spent_today(conn, "mlbb", JAKARTA) == Decimal(0)


def test_the_day_boundary_follows_the_operators_timezone(db):
    with db.begin() as conn:
        spend(conn, Decimal("1.00"))
        conn.execute(
            sa.text("UPDATE costs SET created_at = TIMESTAMPTZ '2026-10-21 17:30:00+00'")
        )  # 00:30 on 22 Oct in Jakarta
    with db.connect() as conn:
        assert ledger.spent_on(conn, "mlbb", date(2026, 10, 22), JAKARTA) == Decimal("1.00")
        assert ledger.spent_on(conn, "mlbb", date(2026, 10, 22), "UTC") == Decimal(0)
        assert ledger.spent_on(conn, "mlbb", date(2026, 10, 21), "UTC") == Decimal("1.00")


def test_a_profile_is_over_budget_at_its_limit_not_before(db):
    with db.begin() as conn:
        spend(conn, Decimal("1.49"))
    with db.connect() as conn:
        assert not ledger.over_budget(conn, "mlbb", Decimal("1.50"), JAKARTA)
    with db.begin() as conn:
        spend(conn, Decimal("0.01"))
    with db.connect() as conn:
        assert ledger.over_budget(conn, "mlbb", Decimal("1.50"), JAKARTA)


def test_a_negative_cost_is_rejected(db):
    with violation(CHECK), db.begin() as conn:
        spend(conn, Decimal("-0.01"))


def test_transcription_seconds_are_recorded_as_units(db):
    with db.begin() as conn:
        spend(conn, Decimal(0), service="understand", units=600, unit="audio_s")
    with db.connect() as conn:
        assert ledger.units_today(conn, "understand", "audio_s", JAKARTA) == Decimal(600)
