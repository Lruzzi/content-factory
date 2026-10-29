"""Which videos are worth fetching, in which order, how many, and which stretch of each. Pure: every number comes from
the profile's `discover` settings."""

from __future__ import annotations

from datetime import datetime, timedelta

import config_schema as cs
from services.discover.youtube import Video

PUBLIC = {"public", "unlisted"}  # members-only, private and the rest are never fetched
NEW_FOR = timedelta(days=2)
SHORT_S = 60
STEP_S = 60


def skip_reason(video: Video, rules: cs.Discover, *, known: set[str]) -> str | None:
    """Why a video from the channel's list is not wanted, or None. Needs nothing but the list."""
    if video.id in known:
        return "already processed"
    if video.live_status in {"is_live", "is_upcoming"}:
        return "live or upcoming (the live watcher's job)"
    if video.duration_s is None:
        return "duration unknown"
    if not rules.min_duration_s <= video.duration_s <= rules.max_duration_s:
        return f"duration out of range ({video.duration_s:.0f} s)"
    if video.duration_s <= SHORT_S and not rules.include_shorts:
        return "a short"
    title = video.title.casefold()
    if any(word.casefold() in title for word in rules.title_exclude):
        return "title excluded"
    if rules.title_include and not any(word.casefold() in title for word in rules.title_include):
        return "title not wanted"
    return None


def detail_skip_reason(video: Video, rules: cs.Discover, now: datetime) -> str | None:
    """The same, with what the video's own page says: who may watch it, and when it was made."""
    if video.availability not in PUBLIC:
        return f"not public ({video.availability})"
    if video.live_status == "post_live":
        return "still processing"
    if video.live_status in {"is_live", "is_upcoming"}:
        return "live or upcoming (the live watcher's job)"
    if video.uploaded is None:
        return "no upload date"
    if now - video.uploaded > timedelta(days=rules.max_age_days):
        return f"too old ({(now - video.uploaded).days} days)"
    return None


def rank(videos: list[Video], now: datetime) -> list[Video]:
    """A video from the last two days first (the newest first), then the rest, most viewed first."""
    fresh = [v for v in videos if v.uploaded is not None and now - v.uploaded <= NEW_FOR]
    rest = [v for v in videos if v not in fresh]
    fresh.sort(key=lambda v: v.uploaded or now, reverse=True)
    rest.sort(key=lambda v: v.views, reverse=True)
    return fresh + rest


def allowance(rules: cs.Discover, *, ever: int, last_day: int) -> int:
    """How many more videos this source may take now: the backfill number the first time, the daily number after."""
    if ever == 0:
        return rules.backfill_per_source
    return max(0, rules.daily_per_source - last_day)


def best_window(
    heat: tuple[tuple[float, float, float], ...] | list[tuple[float, float, float]], duration_s: float, window_s: int
) -> tuple[int, int]:
    """(start, length) in seconds of the stretch to fetch: the video whole if it fits, else the busiest `window_s` of
    YouTube's "most replayed" graph, else a stretch starting a little in (the first minutes are chatter)."""
    if window_s >= duration_s:
        return 0, int(duration_s)
    latest = int((duration_s - window_s) // STEP_S * STEP_S)
    if not heat:
        return min(int(duration_s * 0.15) // STEP_S * STEP_S, latest), window_s
    steps = int(duration_s // STEP_S) + 1
    heat_at = [0.0] * steps  # the average of the graph in each minute
    for a, b, v in heat:
        for j in range(int(a // STEP_S), min(steps, int(b // STEP_S) + 1)):
            overlap = max(0.0, min(b, (j + 1) * STEP_S) - max(a, j * STEP_S))
            heat_at[j] += v * overlap / STEP_S
    span = window_s // STEP_S
    best_start, best_score = 0, -1.0
    for start in range(0, latest + 1, STEP_S):
        # a minute counts up to half as much again towards the middle of the window: the busy part sits in the middle
        score = sum(heat_at[start // STEP_S + k] * (1 + 0.5 * (1 - abs(2 * (k + 0.5) / span - 1))) for k in range(span))
        if score > best_score:
            best_start, best_score = start, score
    return best_start, window_s
