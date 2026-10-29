"""LLM calls are recorded in the cost ledger, so the daily budget stop can see them (FR-13)."""

from __future__ import annotations

from decimal import Decimal

import pytest

import config_schema as cs
from common import ledger
from common.llm import LLMResult, cost

pytestmark = pytest.mark.integration

JAKARTA = "Asia/Jakarta"
SPEC = cs.LLMSpec(provider="anthropic", model="claude-sonnet-5-5")
PRICES = {"anthropic/claude-sonnet-5-5": cs.Price(input=2.0, output=10.0)}


def test_a_call_is_recorded_as_input_and_output_rows_and_counts_toward_the_budget(db):
    with db.begin() as conn:
        total = cost.record(
            conn,
            profile="mlbb",
            service="select",
            spec=SPEC,
            result=LLMResult("x", 500_000, 100_000, "m"),
            prices=PRICES,
        )
    assert total == Decimal("2.00")  # 1.00 for input, 1.00 for output
    with db.connect() as conn:
        assert ledger.spent_today(conn, "mlbb", JAKARTA) == Decimal("2.00")
        assert ledger.units_today(conn, "select", "input_tokens", JAKARTA) == Decimal(500_000)
        assert ledger.units_today(conn, "select", "output_tokens", JAKARTA) == Decimal(100_000)


def test_the_profile_goes_over_budget_once_its_calls_add_up(db):
    with db.begin() as conn:
        for _ in range(3):
            cost.record(
                conn,
                profile="mlbb",
                service="select",
                spec=SPEC,
                result=LLMResult("x", 400_000, 100_000, "m"),
                prices=PRICES,
            )
    with db.connect() as conn:  # 3 x $1.80 = $5.40
        assert ledger.over_budget(conn, "mlbb", Decimal("5.00"), JAKARTA)
        assert not ledger.over_budget(conn, "mlbb", Decimal("6.00"), JAKARTA)


def test_a_free_model_still_records_its_tokens(db):
    free_spec = cs.LLMSpec(provider="anthropic", model="free-model")
    prices = {"anthropic/free-model": cs.Price(input=0, output=0)}
    with db.begin() as conn:
        cost.record(
            conn, profile="mlbb", service="select", spec=free_spec, result=LLMResult("x", 900, 90, "m"), prices=prices
        )
    with db.connect() as conn:
        assert ledger.spent_today(conn, "mlbb", JAKARTA) == Decimal(0)
        assert ledger.units_today(conn, "select", "input_tokens", JAKARTA) == Decimal(900)


def test_an_unpriced_model_is_refused_before_anything_is_written(db):
    from common.llm import LLMError

    with pytest.raises(LLMError, match="claude-sonnet-5-5"), db.begin() as conn:
        cost.record(conn, profile="mlbb", service="select", spec=SPEC, result=LLMResult("x", 1, 1, "m"), prices={})
    with db.connect() as conn:
        assert ledger.units_today(conn, "select", "input_tokens", JAKARTA) == Decimal(0)


def test_the_price_of_the_model_that_actually_answered_is_used_not_the_primarys(db):
    prices = PRICES | {"anthropic/claude-haiku-5-5": cs.Price(input=0.1, output=0.5)}
    served = LLMResult("x", 1_000_000, 1_000_000, "m", price_key="anthropic/claude-haiku-5-5")
    with db.begin() as conn:
        total = cost.record(conn, profile="mlbb", service="select", spec=SPEC, result=served, prices=prices)
    assert total == Decimal("0.60")  # Haiku, not the Sonnet primary's $12.00


def test_the_served_model_alone_is_enough_when_no_spec_is_given(db):
    served = LLMResult("x", 1_000_000, 0, "m", price_key="anthropic/claude-sonnet-5-5")
    with db.begin() as conn:
        assert cost.record(conn, profile="mlbb", service="select", result=served, prices=PRICES) == Decimal("2.00")


def test_a_result_with_no_price_key_and_no_spec_is_refused(db):
    from common.llm import LLMError

    with pytest.raises(LLMError, match="which model"), db.begin() as conn:
        cost.record(conn, profile="mlbb", service="select", result=LLMResult("x", 1, 1, "m"), prices=PRICES)
