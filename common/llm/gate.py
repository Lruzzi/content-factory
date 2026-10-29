"""The gate between a service and its LLMs. When every provider of a chain is out of quota, the first job to find out
raises ONE alert and pauses the chain in Postgres; every other job (in any service) then waits quietly, without calling
anyone, until the provider's own reset time, and loses no attempt. Without it each of them failed three times, was
declared dead, and sent an alert of its own."""

from __future__ import annotations

import logging
from collections.abc import Callable

import sqlalchemy as sa

import config_schema as cs
from common import jobs, pauses, spend
from common.llm.base import LLMError

log = logging.getLogger(__name__)
MIN_WAIT_S = 300  # a paused chain is looked at again no sooner than this
MAX_WAIT_S = 12 * 3600  # nor later than this, whatever a provider claims
GUESS_WAIT_S = 1800  # when no provider said how long
FLOOR_S = 60
SPEND_KEY = "spend:openrouter"
SPEND_RECHECK_S = 600  # a stopped budget is looked at again after this, so a raised cap takes effect within minutes
ALERT_MEMORY_S = 35 * 86400  # the 80% alert comes once a month
OPENROUTER = "openrouter.ai"


def key_of(judge: cs.LLMJudge) -> str:
    return "llm:" + "|".join(cs.llm_price_key(spec) for spec in (judge, *judge.fallbacks))


def _paid_openrouter(judge: cs.LLMJudge, prices: dict[str, cs.Price]) -> bool:
    """Does this chain call a model that costs money through OpenRouter?"""
    for spec in (judge, *judge.fallbacks):
        price = prices.get(cs.llm_price_key(spec))
        if OPENROUTER in (spec.base_url or "") and price is not None and price.input + price.output > 0:
            return True
    return False


class Gate:
    def __init__(
        self,
        engine: sa.Engine,
        alert: Callable[[str], object] | None = None,
        *,
        prices: dict[str, cs.Price] | None = None,
        limits: spend.Limits | None = None,
    ) -> None:
        self._engine, self._alert = engine, alert
        self._prices, self._limits = prices, limits

    def check(self, judge: cs.LLMJudge) -> None:
        """Raise `jobs.Deferred` when this chain is paused, so the caller does not even try."""
        with self._engine.connect() as conn:
            pause = pauses.active(conn, key_of(judge))
        if pause is not None:
            raise jobs.Deferred(max(FLOOR_S, pause.remaining_s), pause.reason)
        self._check_budget(judge)

    def _check_budget(self, judge: cs.LLMJudge) -> None:
        """A chain with a paid OpenRouter model waits once the month's budget is used up (the free tiers carry on)."""
        if self._limits is None or self._prices is None or not _paid_openrouter(judge, self._prices):
            return
        with self._engine.begin() as conn:
            budget = spend.load(conn, self._limits)
            if not budget.alert_due:
                return
            note = f"${float(budget.spent_usd):.2f} of the ${budget.cap_usd:.2f} cap ({budget.share:.0%})"
            if not budget.stopped:
                if pauses.start(conn, f"{SPEND_KEY}:alert:{budget.month}", ALERT_MEMORY_S, "alert sent"):
                    self._send(f"OpenRouter budget at {budget.share:.0%}: {note}. Change the cap with /budget.")
                return
            reason = (
                f"the OpenRouter budget is used up: {note}; jobs that need a paid model wait until the month "
                "rolls over or the cap is raised"
            )
            is_new = pauses.start(conn, SPEND_KEY, SPEND_RECHECK_S, reason)
        if is_new:
            self._send(f"OpenRouter budget stopped: {note}. Jobs wait and nothing is lost. Raise the cap with /budget.")
        raise jobs.Deferred(SPEND_RECHECK_S, reason)

    def _send(self, text: str) -> None:
        if self._alert is None:
            return
        try:
            self._alert(text)
        except Exception:
            log.exception("could not send the budget alert")

    def trip(self, judge: cs.LLMJudge, error: LLMError) -> jobs.Deferred:
        """The chain just answered 'out of quota': pause it, tell the operator once, and return what the job should
        raise to wait."""
        wait = min(
            max(error.retry_after_s if error.retry_after_s is not None else GUESS_WAIT_S, MIN_WAIT_S), MAX_WAIT_S
        )
        reason = f"every LLM provider is out of quota; waiting about {wait / 60:.0f} min before trying again"
        key = key_of(judge)
        with self._engine.begin() as conn:
            is_new = pauses.start(conn, key, wait, reason)
            pause = pauses.active(conn, key)
        if is_new and self._alert is not None:
            try:
                self._alert(
                    f"LLM quota used up: {reason}. Jobs that need it wait on their own and nothing is lost; "
                    "add credit to a provider (or wait for the daily reset) to speed this up."
                )
            except Exception:
                log.exception("could not send the quota alert")
        return jobs.Deferred(max(FLOOR_S, pause.remaining_s if pause else wait), reason)
