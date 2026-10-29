"""What an LLM call costs, and writing it into the cost ledger so the daily budget stop can see it (FR-13)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import sqlalchemy as sa

import config_schema as cs
from common import ledger
from common.llm.base import LLMError, LLMResult

PER_MILLION = Decimal(1_000_000)


def price_for_key(key: str, prices: dict[str, cs.Price]) -> cs.Price:
    try:
        return prices[key]
    except KeyError:
        raise LLMError(f"no price for {key} in llm_prices.yaml (use 0 for a free model)") from None


def price_for(spec: cs.LLMSpec, prices: dict[str, cs.Price]) -> cs.Price:
    return price_for_key(cs.llm_price_key(spec), prices)


def usd(result: LLMResult, price: cs.Price) -> tuple[Decimal, Decimal]:
    """(input dollars, output dollars): tokens times the price per million."""
    return (
        Decimal(result.input_tokens) * Decimal(str(price.input)) / PER_MILLION,
        Decimal(result.output_tokens) * Decimal(str(price.output)) / PER_MILLION,
    )


def record(
    conn: sa.Connection,
    *,
    profile: str,
    service: str,
    result: LLMResult,
    prices: dict[str, cs.Price],
    spec: cs.LLMSpec | None = None,
    job_id: uuid.UUID | None = None,
) -> Decimal:
    """Write one ledger row for the input tokens and one for the output tokens. Returns the total in dollars.

    The price is that of the model that actually answered (`result.price_key`), which with a fallback chain is not
    always the primary. `spec` is only the fallback when a result carries no key. An unpriced model is refused
    before anything is written."""
    key = result.price_key or (cs.llm_price_key(spec) if spec else None)
    if key is None:
        raise LLMError("cannot tell which model to price: the result carries no price_key and no spec was given")
    input_usd, output_usd = usd(result, price_for_key(key, prices))
    for units, unit, dollars in (
        (result.input_tokens, "input_tokens", input_usd),
        (result.output_tokens, "output_tokens", output_usd),
    ):
        ledger.record_cost(
            conn,
            profile=profile,
            service=service,
            provider=key.split("/", 1)[0],
            units=units,
            unit=unit,
            usd=dollars,
            job_id=job_id,
        )
    return input_usd + output_usd
