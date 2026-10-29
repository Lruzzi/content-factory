"""The hours planner. The day's Groq audio is shared fairly between the sources that are active, and what is left of
today's OpenRouter budget caps it too. Both only make a segment WAIT (the caller defers it: no attempt used, nothing
lost), and neither holds anything back that nobody else needs:

- a source over its fair share waits only while another source that is BELOW its share has segments waiting;
- the money limit is in audio seconds: what is left to spend today divided by what an hour of audio costs, measured from
  the cost ledger once there are enough hours to measure, until then the assumed figure from workers.yaml.

"Active" means it sent audio to Groq in the last 24 hours or has a segment waiting now, so sources that are not
streaming do not shrink anyone's share, and a new one is counted as soon as it has work. Windows are rolling, like
`budget.py`'s. Times come from the database clock."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from common import spend
from services.understand import budget

MIN_COST_PER_HOUR = 0.001  # free models measure as 0: never divide by it
WEEK_S = 7 * 86400
FAIR_WAIT_S = 600
MONEY_WAIT_S = 1800


@dataclass(frozen=True)
class Config:
    enabled: bool
    assumed_cost_per_audio_hour_usd: float
    min_measured_hours: float


@dataclass(frozen=True)
class Plan:
    groq_cap_s: float  # the day's audio up to Groq's stop
    sources_active: int
    fair_s: float  # each active source's share of it
    cost_per_hour_usd: float
    money_s_left: float | None  # audio seconds today's remaining OpenRouter budget pays for (None: no budget set up)
    days_left: int | None
    usage: dict[uuid.UUID, float]  # audio seconds each source sent in the last 24 hours
    waiting: frozenset[uuid.UUID]  # sources with a segment waiting


def config_from(workers: dict[str, Any]) -> Config | None:
    """`planner` of workers.yaml (already checked by config_schema), or None when there is none."""
    section = workers.get("planner")
    if not section:
        return None
    return Config(
        enabled=bool(section["enabled"]),
        assumed_cost_per_audio_hour_usd=float(section["assumed_cost_per_audio_hour_usd"]),
        min_measured_hours=float(section["min_measured_hours"]),
    )


def cost_per_audio_hour(conn: sa.Connection, cfg: Config) -> float:
    """Dollars of paid calls per hour of audio (to any provider) over the last week; the assumed figure until enough
    audio is in."""
    seconds = conn.execute(
        sa.text(
            "SELECT COALESCE(SUM(units), 0) FROM costs WHERE service = 'understand' "
            "AND unit = 'audio_s' AND created_at > clock_timestamp() - make_interval(secs => :w)"
        ),
        {"w": WEEK_S},
    ).scalar_one()
    hours = float(seconds) / 3600
    if hours < cfg.min_measured_hours or hours <= 0:
        return cfg.assumed_cost_per_audio_hour_usd
    dollars = conn.execute(
        sa.text(
            "SELECT COALESCE(SUM(usd), 0) FROM costs WHERE provider = ANY(:p) "
            "AND created_at > clock_timestamp() - make_interval(secs => :w)"
        ),
        {"p": list(spend.PAID_PROVIDERS), "w": WEEK_S},
    ).scalar_one()
    return max(float(dollars) / hours, MIN_COST_PER_HOUR)


def plan(
    conn: sa.Connection,
    cfg: Config,
    groq: budget.Limits,
    spend_limits: spend.Limits | None,
    source_id: uuid.UUID | None,
) -> Plan:
    usage = {
        r[0]: float(r[1])
        for r in conn.execute(
            sa.text(
                "SELECT j.source_id, SUM(c.units) FROM costs c JOIN jobs j ON j.id = c.job_id "
                "WHERE c.service = 'understand' AND c.provider = 'groq' AND c.unit = 'audio_s' "
                "AND j.source_id IS NOT NULL AND c.created_at > clock_timestamp() - make_interval(secs => :w) "
                "GROUP BY j.source_id"
            ),
            {"w": budget.DAY_S},
        )
    }
    waiting = frozenset(
        r[0]
        for r in conn.execute(
            sa.text(
                "SELECT DISTINCT source_id FROM jobs WHERE stage = 'understand' "
                "AND status IN ('queued', 'running', 'failed') AND source_id IS NOT NULL"
            )
        )
    )
    active = set(usage) | waiting | ({source_id} if source_id is not None else set())
    cap_s = groq.audio_seconds_per_day * groq.stop_at
    cost = cost_per_audio_hour(conn, cfg)
    money_s_left = days_left = None
    if spend_limits is not None:
        b = spend.load(conn, spend_limits)
        money_s_left, days_left = b.remaining_today_usd / cost * 3600, b.days_left
    return Plan(cap_s, max(1, len(active)), cap_s / max(1, len(active)), cost, money_s_left, days_left, usage, waiting)


def check(
    conn: sa.Connection,
    cfg: Config,
    groq: budget.Limits,
    spend_limits: spend.Limits | None,
    source_id: uuid.UUID | None,
    needed_s: float,
) -> budget.Wait | None:
    """None when the segment may go to Groq now, otherwise how long it waits and why."""
    if not cfg.enabled:
        return None
    p = plan(conn, cfg, groq, spend_limits, source_id)
    need = max(needed_s, budget.MIN_REQUEST_S)
    if p.money_s_left is not None and need > p.money_s_left:
        return budget.Wait(
            MONEY_WAIT_S,
            f"today's share of the OpenRouter budget pays for about {p.money_s_left / 3600:.1f} more hours of audio at "
            f"${p.cost_per_hour_usd:.3f} an hour; it waits for tomorrow or a higher cap (/budget)",
        )
    mine = p.usage.get(source_id, 0.0) if source_id is not None else 0.0
    if mine + need <= p.fair_s:
        return None
    others = [s for s in p.waiting if s != source_id and p.usage.get(s, 0.0) < p.fair_s]
    if others:
        return budget.Wait(
            FAIR_WAIT_S,
            f"this source has used its fair share of today's audio ({mine / 3600:.1f} of {p.fair_s / 3600:.1f} h, "
            f"{p.sources_active} sources active) and another source below its share is waiting",
        )
    return None
