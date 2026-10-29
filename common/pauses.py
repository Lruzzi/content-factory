"""A pause: a note in Postgres that something shared is used up until a time (an LLM quota), so every service sees the
same one and only the first to notice raises the alarm. Times come from the database clock."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

SAME_TROUBLE_S = 6 * 3600  # a pause that ended less than this ago and comes back is the same trouble, not a new one


@dataclass(frozen=True)
class Pause:
    key: str
    reason: str
    remaining_s: float


def active(conn: sa.Connection, key: str) -> Pause | None:
    row = conn.execute(
        sa.text(
            "SELECT reason, EXTRACT(EPOCH FROM until - clock_timestamp()) FROM pauses "
            "WHERE key = :k AND until > clock_timestamp()"
        ),
        {"k": key},
    ).first()
    return None if row is None else Pause(key, row[0], float(row[1]))


def start(conn: sa.Connection, key: str, seconds: float, reason: str) -> bool:
    """Pause `key` for `seconds`. True when this is new trouble worth an alert; False when it was already paused
    (nothing changes), or ended only a little ago (it is paused again, quietly)."""
    params = {"k": key, "s": seconds, "r": reason[:500]}
    made = conn.execute(
        sa.text(
            "INSERT INTO pauses (key, until, reason) "
            "VALUES (:k, clock_timestamp() + make_interval(secs => CAST(:s AS double precision)), :r) "
            "ON CONFLICT (tenant_id, key) DO NOTHING RETURNING 1"
        ),
        params,
    ).first()
    if made is not None:
        return True
    ended_ago = conn.execute(
        sa.text("SELECT EXTRACT(EPOCH FROM clock_timestamp() - until) FROM pauses WHERE key = :k FOR UPDATE"),
        {"k": key},
    ).scalar_one()
    if ended_ago < 0:
        return False  # still paused
    conn.execute(
        sa.text(
            "UPDATE pauses SET until = clock_timestamp() + make_interval(secs => CAST(:s AS double precision)), "
            "reason = :r WHERE key = :k"
        ),
        params,
    )
    return float(ended_ago) > SAME_TROUBLE_S


# What you can pause by hand: posting; new live recordings (`live`); discovery of ended streams (`ended`); and `intake`,
# which is both of those at once.
SWITCHES = ("publish", "live", "ended", "intake")


def switch_key(name: str) -> str:
    return f"switch:{name}"


def intake_paused(conn: sa.Connection, part: str) -> bool:
    """Whether new `live` recordings or the discovery of `ended` streams are paused by hand: by their own switch, or by
    `intake`, which stops both. Recordings already under way go on."""
    return any(active(conn, switch_key(name)) is not None for name in ("intake", part))


def hold(conn: sa.Connection, key: str, reason: str) -> None:
    """Pause `key` until someone releases it (it has no end time, so a restart does not undo it)."""
    conn.execute(
        sa.text(
            "INSERT INTO pauses (key, until, reason) VALUES (:k, 'infinity', :r) "
            "ON CONFLICT (tenant_id, key) DO UPDATE SET until = 'infinity', reason = EXCLUDED.reason"
        ),
        {"k": key, "r": reason[:500]},
    )


def release(conn: sa.Connection, key: str) -> bool:
    """End a pause. False when there was none."""
    return conn.execute(sa.text("DELETE FROM pauses WHERE key = :k RETURNING 1"), {"k": key}).first() is not None


def switches_on(conn: sa.Connection) -> list[str]:
    """The names of the hand switches that are on (paused), sorted."""
    keys = conn.execute(
        sa.text("SELECT key FROM pauses WHERE key LIKE 'switch:%' AND until > clock_timestamp() ORDER BY key")
    ).scalars()
    return [k.removeprefix("switch:") for k in keys]
