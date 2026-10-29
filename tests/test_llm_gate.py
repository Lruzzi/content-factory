"""The gate between a service and its LLMs: one alert when the quota runs out, everything else waits quietly."""

from __future__ import annotations

import pytest

import config_schema as cs
from common import jobs
from common.llm import LLMError
from common.llm.gate import Gate

pytestmark = pytest.mark.integration


def judge(**kw) -> cs.LLMJudge:
    base = {
        "provider": "openai_compatible", "model": "a", "base_url": "https://a.example/v1", "api_key_env": "TEST_KEY",
        "prompt": "prompts/default.md",
    }  # fmt: skip
    return cs.LLMJudge(**(base | kw))


def limited(after=1800.0) -> LLMError:
    return LLMError("quota", retryable=True, rate_limited=True, retry_after_s=after)


def test_with_nothing_paused_the_gate_lets_the_call_through(db):
    Gate(db, alert=lambda m: None).check(judge())


def test_a_quota_error_pauses_the_chain_and_gives_the_wait_for_the_job(db):
    alerts: list[str] = []
    gate = Gate(db, alert=alerts.append)
    deferred = gate.trip(judge(), limited(1800.0))
    assert isinstance(deferred, jobs.Deferred) and 1700 <= deferred.wait_s <= 1800
    assert len(alerts) == 1 and "quota" in alerts[0].lower()


def test_after_that_the_gate_stops_calls_before_they_are_made_and_without_another_alert(db):
    alerts: list[str] = []
    gate = Gate(db, alert=alerts.append)
    gate.trip(judge(), limited(1800.0))
    with pytest.raises(jobs.Deferred) as caught:
        gate.check(judge())
    assert 60 <= caught.value.wait_s <= 1800
    gate.trip(judge(), limited(1800.0))  # another job that ran into it meanwhile
    assert len(alerts) == 1


def test_a_second_service_sees_the_same_pause_and_does_not_alert_again(db):
    first: list[str] = []
    second: list[str] = []
    Gate(db, alert=first.append).trip(judge(), limited())
    other = Gate(db, alert=second.append)
    with pytest.raises(jobs.Deferred):
        other.check(judge())
    other.trip(judge(), limited())
    assert len(first) == 1 and second == []


def test_a_different_chain_of_models_is_not_paused(db):
    Gate(db, alert=lambda m: None).trip(judge(), limited())
    Gate(db, alert=lambda m: None).check(judge(model="other"))


def test_the_wait_is_never_shorter_than_five_minutes_nor_longer_than_twelve_hours(db):
    gate = Gate(db, alert=lambda m: None)
    assert gate.trip(judge(model="s"), limited(3.0)).wait_s >= 290
    assert gate.trip(judge(model="l"), limited(10**7)).wait_s <= 12 * 3600 + 5
    assert 290 <= gate.trip(judge(model="n"), limited(None)).wait_s <= 3600  # no hint: a guess, then look again


def test_an_alert_that_cannot_be_sent_does_not_stop_the_wait(db):
    def broken(message):
        raise ConnectionError("telegram is down")

    assert isinstance(Gate(db, alert=broken).trip(judge(), limited()), jobs.Deferred)


# ---------- the OpenRouter budget ----------
from decimal import Decimal  # noqa: E402

from common import ledger, spend  # noqa: E402

LIMITS = spend.Limits(cap_ceiling_usd=17.0, cap_usd=10.0, alert_at=0.8, stop_at=0.9)
PRICES = {
    "openai_compatible/paid": cs.Price(input=0.1, output=0.5),
    "openai_compatible/a": cs.Price(input=0, output=0),
}


def paid(**kw) -> cs.LLMJudge:
    return judge(model="paid", base_url="https://openrouter.ai/api/v1", **kw)


def spent(db, dollars):
    with db.begin() as conn:
        ledger.record_cost(
            conn, profile="mlbb", service="select", provider="openai_compatible", units=Decimal(1),
            unit="input_tokens", usd=Decimal(str(dollars)),
        )  # fmt: skip


def budget_gate(db, alerts):
    return Gate(db, alerts.append, prices=PRICES, limits=LIMITS)


def test_below_the_stop_a_paid_chain_goes_through(db):
    spent(db, 5.0)
    budget_gate(db, []).check(paid())


def test_at_the_alert_share_you_hear_once_and_the_call_still_goes_through(db):
    alerts: list[str] = []
    spent(db, 8.5)  # 85% of the 10 dollar cap
    gate = budget_gate(db, alerts)
    gate.check(paid())
    gate.check(paid())
    assert len(alerts) == 1 and "85%" in alerts[0] and "/budget" in alerts[0]


def test_past_the_stop_a_paid_chain_waits_with_one_alert_and_nothing_is_called(db):
    alerts: list[str] = []
    spent(db, 9.5)
    gate = budget_gate(db, alerts)
    with pytest.raises(jobs.Deferred) as caught:
        gate.check(paid())
    assert caught.value.wait_s >= 60 and "budget" in caught.value.reason.lower()
    with pytest.raises(jobs.Deferred):
        gate.check(paid())
    assert len(alerts) == 1 and "stopped" in alerts[0].lower()


def test_a_chain_of_free_models_is_never_stopped_by_the_budget(db):
    spent(db, 9.9)
    budget_gate(db, []).check(judge())  # model a costs nothing
    budget_gate(db, []).check(judge(model="paid", base_url="https://elsewhere.example/v1"))  # not through OpenRouter


def test_a_paid_fallback_counts_as_a_paid_chain(db):
    spent(db, 9.9)
    chain = judge(fallbacks=[{"provider": "openai_compatible", "model": "paid", "base_url": "https://openrouter.ai/api/v1",
                              "api_key_env": "TEST_KEY"}])  # fmt: skip
    with pytest.raises(jobs.Deferred):
        budget_gate(db, []).check(chain)


def test_raising_the_cap_lets_the_jobs_through_at_once(db):
    spent(db, 9.5)
    gate = budget_gate(db, [])
    with pytest.raises(jobs.Deferred):
        gate.check(paid())
    with db.begin() as conn:
        spend.set_cap(conn, LIMITS, 17.0, by="telegram:1")
    gate.check(paid())  # 9.5 of 17 is 56%: well below the stop


def test_without_limits_or_prices_the_gate_behaves_as_before(db):
    spent(db, 99.0)
    Gate(db, lambda m: None).check(paid())
