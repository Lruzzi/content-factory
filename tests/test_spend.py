"""The monthly budget for the paid OpenRouter key: the cap you set from Telegram (never above the ceiling in config),
the month's spend from the cost ledger, what that leaves per day, and the stop (nothing is lost: jobs wait)."""

from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa

from common import ledger, spend

pytestmark = pytest.mark.integration

LIMITS = spend.Limits(cap_ceiling_usd=17.0, cap_usd=17.0, alert_at=0.8, stop_at=0.9)


def pay(db, dollars, provider="openai_compatible", ago_days=0):
    with db.begin() as conn:
        ledger.record_cost(
            conn, profile="mlbb", service="select", provider=provider, units=Decimal(1000), unit="input_tokens",
            usd=Decimal(str(dollars)),
        )  # fmt: skip
        conn.execute(sa.text("UPDATE costs SET created_at = created_at - make_interval(days => :d)"), {"d": ago_days})


def load(db, limits=LIMITS):
    with db.connect() as conn:
        return spend.load(conn, limits)


def test_with_nothing_set_the_cap_is_the_one_in_config_and_nothing_is_spent(db):
    b = load(db)
    assert (b.cap_usd, float(b.spent_usd), b.stopped, b.alert_due) == (17.0, 0.0, False, False)


def test_the_months_spend_counts_paid_openai_compatible_calls_and_nothing_else(db):
    pay(db, 2.5)
    pay(db, 1.0, provider="openrouter")
    pay(db, 9.0, provider="groq")  # a different bill
    pay(db, 9.0, provider="anthropic")  # so is this one
    assert float(load(db).spent_usd) == pytest.approx(3.5)


def test_last_months_spend_does_not_count(db):
    pay(db, 5.0, ago_days=40)
    assert float(load(db).spent_usd) == 0.0


def test_the_alert_is_due_at_the_alert_share_and_the_stop_at_the_stop_share(db):
    pay(db, 13.7)  # 80.6% of 17
    b = load(db)
    assert b.alert_due and not b.stopped
    pay(db, 1.7)  # 15.4 of 17: past 90%
    assert load(db).stopped


def test_what_is_left_per_day_is_the_room_below_the_stop_spread_over_the_days_left(db):
    pay(db, 5.0)
    b = load(db)
    assert b.days_left >= 1
    assert b.daily_allowance == pytest.approx((17.0 * 0.9 - 5.0) / b.days_left)


def test_nothing_is_left_per_day_once_stopped(db):
    pay(db, 16.0)
    assert load(db).daily_allowance == 0.0


def test_a_cap_you_set_wins_over_config_and_is_remembered(db):
    with db.begin() as conn:
        assert spend.set_cap(conn, LIMITS, 12.0, by="telegram:1").cap_usd == 12.0
    assert load(db).cap_usd == 12.0


@pytest.mark.parametrize("bad", [0, -1, 17.01, 100, float("nan"), float("inf"), True])
def test_a_cap_outside_zero_and_the_ceiling_is_refused_and_changes_nothing(db, bad):
    with pytest.raises(ValueError, match="cap"), db.begin() as conn:
        spend.set_cap(conn, LIMITS, bad, by="telegram:1")
    assert load(db).cap_usd == 17.0


def test_a_stored_cap_above_a_lowered_ceiling_is_cut_down_to_the_ceiling(db):
    with db.begin() as conn:
        spend.set_cap(conn, LIMITS, 17.0, by="telegram:1")
    lowered = spend.Limits(cap_ceiling_usd=10.0, cap_usd=10.0, alert_at=0.8, stop_at=0.9)
    assert load(db, lowered).cap_usd == 10.0


def test_a_garbled_stored_value_falls_back_to_config(db):
    with db.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO settings (key, value) VALUES (:k, CAST('\"lots\"' AS jsonb))"), {"k": spend.KEY}
        )
    assert load(db).cap_usd == 17.0


def test_who_changed_the_cap_and_when_is_kept(db):
    with db.begin() as conn:
        spend.set_cap(conn, LIMITS, 14.0, by="telegram:42")
    with db.connect() as conn:
        row = conn.execute(
            sa.text("SELECT updated_by, updated_at FROM settings WHERE key = :k"), {"k": spend.KEY}
        ).one()
    assert row[0] == "telegram:42" and row[1] is not None


def test_the_limits_come_from_the_budget_section_of_workers_yaml():
    section = {"cap_ceiling_usd": 17, "cap_usd": 15, "alert_at": 0.8, "stop_at": 0.9}
    assert spend.limits_from({"budget": {"openrouter": section}}) == spend.Limits(17.0, 15.0, 0.8, 0.9)
    assert spend.limits_from({}) is None


def test_the_repos_workers_yaml_sets_a_budget_with_a_typo_guard_well_above_the_start():
    import yaml

    import config_schema as cs

    limits = spend.limits_from(yaml.safe_load((cs.ROOT / "workers.yaml").read_text()))
    assert limits is not None and limits.cap_ceiling_usd == 100.0 and limits.alert_at < limits.stop_at <= 1


# ---------- workers.yaml: the budget section ----------
@pytest.mark.parametrize(
    "bad",
    [
        {"cap_usd": 0},
        {"cap_usd": 101},  # above the ceiling
        {"cap_ceiling_usd": -1},
        {"step_usd": 1},  # the +/- buttons are gone
        {"alert_at": 0},
        {"alert_at": 0.95},  # above the stop
        {"stop_at": 1.2},
        {"stop_at": True},
        {"extra": 1},
    ],
)
def test_a_wrong_budget_section_is_refused(tmp_path, bad):
    import config_schema as cs
    from profile_factory import workers_file

    path = workers_file(tmp_path, lambda w: w["budget"]["openrouter"].update(bad))
    with pytest.raises(ValueError, match="budget"):
        cs.validate_workers(path)


def test_the_budget_section_is_optional(tmp_path):
    import config_schema as cs
    from profile_factory import workers_file

    cs.validate_workers(workers_file(tmp_path, lambda w: w.pop("budget")))
