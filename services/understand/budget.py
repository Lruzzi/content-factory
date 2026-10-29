"""Staying inside Groq's free limits. The audio seconds already sent are in the cost ledger (one row per transcription),
so the last hour and the last 24 hours can be added up before the next segment is sent. If sending it would pass a
limit, the job waits instead of failing. The windows are counted as rolling, which is an assumption: Groq does not say
whether its daily limit is rolling or resets at a fixed time, so treat these numbers as an early warning, not as the
truth. The Groq console shows the official figures."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

MIN_REQUEST_S = 10  # Groq bills a request as at least 10 seconds of audio
HOUR_S, DAY_S = 3600, 86400


@dataclass(frozen=True)
class Limits:
    audio_seconds_per_hour: int
    audio_seconds_per_day: int
    alert_at: float  # share of the daily limit at which you are told
    stop_at: float = 1.0  # share of the daily limit at which transcription waits, so the limit itself is never reached


@dataclass(frozen=True)
class Wait:
    wait_s: float
    reason: str


def used(conn: sa.Connection, window_s: int) -> float:
    """Audio seconds sent to Groq in the last `window_s` seconds."""
    total = conn.execute(
        sa.text(
            "SELECT COALESCE(SUM(units), 0) FROM costs WHERE service = 'understand' AND provider = 'groq' "
            "AND unit = 'audio_s' AND created_at > clock_timestamp() - make_interval(secs => :w)"
        ),
        {"w": window_s},
    ).scalar_one()
    return float(total)


def check(conn: sa.Connection, limits: Limits, needed_s: float) -> Wait | None:
    """None when the next segment fits under both limits, otherwise how long to wait and why."""
    need = max(needed_s, MIN_REQUEST_S)
    day = used(conn, DAY_S)
    if day + need > limits.audio_seconds_per_day:
        return Wait(1800, f"the daily Groq limit of {limits.audio_seconds_per_day} audio seconds would be passed")
    if day + need > limits.audio_seconds_per_day * limits.stop_at:
        return Wait(
            1800,
            f"Groq audio use would pass {limits.stop_at:.0%} of the daily limit "
            f"({day / limits.audio_seconds_per_day:.0%} is used now); it waits until the last 24 hours use less",
        )
    if used(conn, HOUR_S) + need > limits.audio_seconds_per_hour:
        return Wait(300, f"the hourly Groq limit of {limits.audio_seconds_per_hour} audio seconds would be passed")
    return None


def day_share(conn: sa.Connection, limits: Limits) -> float:
    return used(conn, DAY_S) / limits.audio_seconds_per_day


def report(conn: sa.Connection, limits: Limits) -> str:
    hour, day = used(conn, HOUR_S), used(conn, DAY_S)
    return (
        f"Groq audio sent, counted from the cost ledger (rolling windows):\n"
        f"  last hour:     {hour:.0f} of {limits.audio_seconds_per_hour} s "
        f"({hour / limits.audio_seconds_per_hour:.0%})\n"
        f"  last 24 hours: {day:.0f} of {limits.audio_seconds_per_day} s "
        f"({day / limits.audio_seconds_per_day:.0%})"
        + (f"\n  transcription waits at {limits.stop_at:.0%} of the daily limit" if limits.stop_at < 1 else "")
    )
