"""Posting and production rules you can change while the system runs, from Telegram: how many posts an account may
make in 24 hours, the time between posts, the posting windows, and how many clips are made in 24 hours. The values
in config (accounts.yaml `defaults`, the profile's `selection.clips_per_day`) are the starting point; a value you set
is a row in `settings` with who set it and wins over config until you set it again. `workers.yaml` `rules.ceilings`
is the guard against a typo or a stolen Telegram account: nothing can be set above it. Times use the database clock."""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass
from datetime import time
from typing import Any

import sqlalchemy as sa

KEYS = {
    "cap": "rules.daily_cap",
    "spacing": "rules.min_spacing_min",
    "clips": "rules.clips_per_day",
    "window": "rules.post_windows",
}
FIELDS = {
    "cap": "daily_cap",
    "spacing": "min_spacing_min",
    "window": "post_windows",
}  # AccountRules fields it overrides
MAX_WINDOWS = 4
WINDOW = re.compile(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})")


@dataclass(frozen=True)
class Ceilings:
    daily_cap: int
    min_spacing_min: int
    clips_per_day: int


def ceilings_from(workers: dict[str, Any]) -> Ceilings | None:
    """`rules.ceilings` of workers.yaml (already checked by config_schema), or None when there is none."""
    section = (workers.get("rules") or {}).get("ceilings")
    if not section:
        return None
    return Ceilings(int(section["daily_cap"]), int(section["min_spacing_min"]), int(section["clips_per_day"]))


def parse_window(text: str) -> tuple[time, time]:
    """`18:00-22:00` as two times; raises ValueError when it is not a time range."""
    match = WINDOW.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"posting window {text!r} must look like 18:00-22:00")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    if not (h1 < 24 and h2 < 24 and m1 < 60 and m2 < 60) or (h1, m1) == (h2, m2):
        raise ValueError(f"posting window {text!r} is not a valid time range")
    return time(h1, m1), time(h2, m2)


def parse(name: str, text: str, ceilings: Ceilings) -> int | list[str]:
    """The value of a typed rule, or ValueError saying what is wrong. Numbers are whole; `window` is one or more
    ranges separated by commas, or `any` for no window at all."""
    text = text.strip()
    if name == "window":
        if text.lower() == "any":
            return []
        parts = [p.strip() for p in text.split(",")]
        if len(parts) > MAX_WINDOWS:
            raise ValueError(f"at most {MAX_WINDOWS} windows")
        for part in parts:
            parse_window(part)
        return parts
    if not re.fullmatch(r"\d{1,4}", text):
        raise ValueError("send a whole number")
    value = int(text)
    low, high = {
        "cap": (1, ceilings.daily_cap),
        "spacing": (0, ceilings.min_spacing_min),
        "clips": (0, ceilings.clips_per_day),
    }[name]
    if not low <= value <= high:
        raise ValueError(f"that must be from {low} to {high} (the ceiling in config)")
    return value


def encode(name: str, value: int | list[str]) -> str:
    """A value as it goes in a button (Telegram allows 64 bytes there): no colons, windows joined with commas."""
    return ",".join(w.replace(":", "") for w in value) if isinstance(value, list) else str(value)


def decode(name: str, text: str, ceilings: Ceilings) -> int | list[str]:
    if name == "window":
        if text == "any":
            return []
        return [f"{p[:2]}:{p[2:4]}-{p[5:7]}:{p[7:9]}" for p in text.split(",")]
    return parse(name, text, ceilings)


def overrides(conn: sa.Connection) -> dict[str, tuple[Any, str]]:
    """What you set from Telegram: name -> (value, who)."""
    rows = conn.execute(sa.text("SELECT key, value, updated_by FROM settings WHERE key LIKE 'rules.%'")).all()
    by_key = {r[0]: (r[1], r[2]) for r in rows}
    return {name: by_key[key] for name, key in KEYS.items() if key in by_key}


def apply[T](conn: sa.Connection, account_rules: T) -> T:
    """The account's rules (a dataclass with daily_cap, min_spacing_min and post_windows) with what you set from
    Telegram on top. A value that is not usable (a hand-edited row) is ignored, so the config value stands."""
    changes: dict[str, Any] = {}
    for name, (value, _by) in overrides(conn).items():
        field = FIELDS.get(name)
        if field is None:
            continue
        if name == "window" and isinstance(value, list) and all(isinstance(w, str) for w in value):
            try:
                for w in value:
                    parse_window(w)
            except ValueError:
                continue
            changes[field] = list(value)
        elif name != "window" and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            changes[field] = value
    return dataclasses.replace(account_rules, **changes) if changes else account_rules  # type: ignore[type-var]


def clips_per_day(conn: sa.Connection, default: int) -> int:
    """How many clips may be made in 24 hours (0: no limit): what you set, else the profile's value."""
    found = overrides(conn).get("clips")
    if found is not None and isinstance(found[0], int) and not isinstance(found[0], bool) and found[0] >= 0:
        return int(found[0])
    return default


def clips_made(conn: sa.Connection, hours: int = 24) -> int:
    """Clips made in the last 24 hours that are still going to be posted: not a rejected one, a test clip, or a clip
    of a blocked source (a replay)."""
    return int(
        conn.execute(
            sa.text(
                "SELECT count(*) FROM jobs WHERE data ? 'clip' "
                "AND created_at > clock_timestamp() - make_interval(hours => :h) "
                "AND COALESCE(data->'review'->>'state', '') <> 'rejected' "
                "AND COALESCE(data->'clip'->>'test', '') <> 'true' "
                "AND COALESCE(spec->'source'->>'clip_policy', '') <> 'blocked'"
            ),
            {"h": hours},
        ).scalar_one()
    )


def clips_left(conn: sa.Connection, default: int) -> int | None:
    """How many more clips may be made now, or None when there is no limit."""
    limit = clips_per_day(conn, default)
    return None if limit == 0 else max(0, limit - clips_made(conn))


def set_value(conn: sa.Connection, name: str, value: int | list[str], ceilings: Ceilings, by: str) -> None:
    """Keep a rule. Refused (nothing changes) when the value is outside what the ceilings allow."""
    if name == "window":
        if not isinstance(value, list) or len(value) > MAX_WINDOWS:
            raise ValueError("windows must be a list of ranges")
        for w in value:
            parse_window(w)
    else:
        parse(name, str(value), ceilings)
    conn.execute(
        sa.text(
            "INSERT INTO settings (key, value, updated_by) VALUES (:k, CAST(:v AS jsonb), :by) "
            "ON CONFLICT (tenant_id, key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, "
            "updated_at = now()"
        ),
        {"k": KEYS[name], "v": json.dumps(value), "by": by[:100]},
    )
