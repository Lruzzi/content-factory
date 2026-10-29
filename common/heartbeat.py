"""Every service says it is alive every half minute (a row per service and instance in `heartbeats`). A service that
goes quiet can then be told from one that is merely idle, without anything reaching into Docker. Times come from the
database clock."""

from __future__ import annotations

import logging
import socket
import threading
from dataclasses import dataclass

import sqlalchemy as sa

log = logging.getLogger(__name__)
EVERY_S = 30.0


@dataclass(frozen=True)
class ServiceState:
    name: str
    up: bool | None  # None: never reported
    age_s: float | None  # seconds since its latest beat


def beat(conn: sa.Connection, service: str, instance: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO heartbeats (service, instance) VALUES (:s, :i) "
            "ON CONFLICT (tenant_id, service, instance) DO UPDATE SET beat_at = clock_timestamp()"
        ),
        {"s": service, "i": instance},
    )


def start(
    engine: sa.Engine,
    service: str,
    *,
    instance: str | None = None,
    every_s: float = EVERY_S,
    stop: threading.Event | None = None,
) -> threading.Thread:
    """Beat now and then every `every_s` seconds in a background thread, until `stop` is set. A database that is briefly
    gone is skipped, never fatal: the next beat tries again."""
    who = instance or socket.gethostname()
    done = stop or threading.Event()

    def run() -> None:
        while not done.is_set():
            try:
                with engine.begin() as conn:
                    beat(conn, service, who)
            except Exception:
                log.warning("could not record a heartbeat of %s; trying again", service)
            done.wait(every_s)

    thread = threading.Thread(target=run, name=f"heartbeat-{service}", daemon=True)
    thread.start()
    return thread


def states(conn: sa.Connection, expected: list[str], *, stale_after_s: float) -> list[ServiceState]:
    """For each expected service (in that order): up if any instance beat within `stale_after_s`, down if not, unknown
    if it never has."""
    rows = {
        r[0]: float(r[1])
        for r in conn.execute(
            sa.text(
                "SELECT service, EXTRACT(EPOCH FROM clock_timestamp() - max(beat_at)) FROM heartbeats "
                "WHERE service = ANY(:names) GROUP BY service"
            ),
            {"names": expected},
        )
    }
    return [
        ServiceState(name, None if name not in rows else rows[name] <= stale_after_s, rows.get(name))
        for name in expected
    ]
