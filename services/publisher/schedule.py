"""When a clip may be posted to an account (FR-9): inside a posting window, under the daily cap, and far enough after
the last post. Pure code: the caller supplies the current time and the times of the account's earlier posts. Windows
are in the account's own time zone (accounts.yaml `timezone`). A "day" for the cap is the last 24 hours, not the
calendar day: platforms count a rolling 24 hours, and a midnight reset would let a full cap go out just before it and
another just after."""

from __future__ import annotations

from datetime import datetime, time, timedelta, tzinfo

from common.rules import parse_window

DAY_H = 24  # how far back the daily cap looks


_window = parse_window


def _inside(at: time, start: time, end: time) -> bool:
    return start <= at < end if start < end else at >= start or at < end  # a window may run past midnight


def _local(moment: datetime, tz: tzinfo) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("a time without a time zone would be a guess: give every time a time zone")
    return moment.astimezone(tz)


def check(
    *, now: datetime, posts: list[datetime], cap: int, spacing_min: int, windows: list[str], tz: tzinfo
) -> str | None:
    """None when posting now is allowed, otherwise the reason it is not."""
    ranges = [_window(w) for w in windows]
    local_now = _local(now, tz)
    earlier = [_local(p, tz) for p in posts]
    if sum(1 for p in earlier if local_now - timedelta(hours=DAY_H) < p <= local_now) >= cap:
        return f"cap of {cap} posts in {DAY_H} hours reached"
    if earlier and local_now < max(earlier) + timedelta(minutes=spacing_min):
        return f"too soon after the last post (spacing is {spacing_min} min)"
    if ranges and not any(_inside(local_now.time(), start, end) for start, end in ranges):
        return "outside the posting windows"
    return None
