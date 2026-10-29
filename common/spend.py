"""The monthly budget for the paid OpenRouter key: the cap now (the one in workers.yaml, or the one you set from
Telegram, never above the ceiling), what the cost ledger says was spent this month, and what that leaves per day.
Times come from the database clock; the month is the UTC month, which is when OpenRouter resets a monthly key limit."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

KEY = "budget.openrouter.cap_usd"
# Ledger providers paid from the OpenRouter key. Every OpenAI-compatible call is filed as `openai_compatible`, so a paid
# Gemini key would be counted too: that errs on the side of stopping early, and today those models are free.
PAID_PROVIDERS = ("openai_compatible", "openrouter")


@dataclass(frozen=True)
class Limits:
    cap_ceiling_usd: float
    cap_usd: float
    alert_at: float
    stop_at: float


@dataclass(frozen=True)
class Budget:
    limits: Limits
    cap_usd: float
    spent_usd: Decimal
    days_left: int  # including today
    month: str  # `2026-10`, the UTC month the spend belongs to
    spent_today_usd: Decimal = Decimal(0)  # since UTC midnight

    @property
    def share(self) -> float:
        return float(self.spent_usd) / self.cap_usd

    @property
    def stop_usd(self) -> float:
        return self.cap_usd * self.limits.stop_at

    @property
    def stopped(self) -> bool:
        return float(self.spent_usd) >= self.stop_usd

    @property
    def alert_due(self) -> bool:
        return self.share >= self.limits.alert_at

    @property
    def remaining_today_usd(self) -> float:
        """What may still be spent today: an even share of what was left below the stop when today began, less what
        has gone today."""
        before_today = float(self.spent_usd - self.spent_today_usd)
        return max(0.0, (self.stop_usd - before_today) / max(1, self.days_left) - float(self.spent_today_usd))

    @property
    def daily_allowance(self) -> float:
        return max(0.0, self.stop_usd - float(self.spent_usd)) / max(1, self.days_left)


def limits_from(workers: dict[str, Any]) -> Limits | None:
    """`budget.openrouter` of workers.yaml (already checked by config_schema), or None when there is none."""
    section = (workers.get("budget") or {}).get("openrouter")
    if not section:
        return None
    return Limits(
        cap_ceiling_usd=float(section["cap_ceiling_usd"]),
        cap_usd=float(section["cap_usd"]),
        alert_at=float(section["alert_at"]),
        stop_at=float(section["stop_at"]),
    )


def _cap(conn: sa.Connection, limits: Limits) -> float:
    stored = conn.execute(sa.text("SELECT value FROM settings WHERE key = :k"), {"k": KEY}).scalar_one_or_none()
    usable = isinstance(stored, (int, float)) and not isinstance(stored, bool) and math.isfinite(stored)
    cap = float(stored) if usable and stored is not None else limits.cap_usd
    return min(cap, limits.cap_ceiling_usd) if cap > 0 else limits.cap_usd


def load(conn: sa.Connection, limits: Limits) -> Budget:
    spent, days_left, month, today = conn.execute(
        sa.text(
            "SELECT COALESCE(SUM(usd), 0), "
            "(date_trunc('month', clock_timestamp() AT TIME ZONE 'UTC') + interval '1 month')::date "
            "- (clock_timestamp() AT TIME ZONE 'UTC')::date, "
            "to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM'), "
            "COALESCE(SUM(usd) FILTER (WHERE created_at >= "
            "date_trunc('day', clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'), 0) "
            "FROM costs WHERE provider = ANY(:p) "
            "AND created_at >= date_trunc('month', clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
        ),
        {"p": list(PAID_PROVIDERS)},
    ).one()
    return Budget(limits, _cap(conn, limits), Decimal(spent), int(days_left), str(month), Decimal(today))


def check_cap(limits: Limits, usd: float) -> None:
    """Raise ValueError unless `usd` is a number above zero and not above the ceiling."""
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not math.isfinite(usd):
        raise ValueError("the cap must be a number")
    if not 0 < usd <= limits.cap_ceiling_usd:
        raise ValueError(f"the cap must be above 0 and at most ${limits.cap_ceiling_usd:g} (the ceiling in config)")


def set_cap(conn: sa.Connection, limits: Limits, usd: float, by: str) -> Budget:
    """Keep a new cap. Refused (nothing changes) when it is not above zero or is above the ceiling in workers.yaml."""
    check_cap(limits, usd)
    conn.execute(
        sa.text(
            "INSERT INTO settings (key, value, updated_by) VALUES (:k, CAST(:v AS jsonb), :by) "
            "ON CONFLICT (tenant_id, key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, "
            "updated_at = now()"
        ),
        {"k": KEY, "v": json.dumps(round(float(usd), 2)), "by": by[:100]},
    )
    return load(conn, limits)
