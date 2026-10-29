"""The publisher service loop. Approved clips arrive on the `publish` stream (one consumer group per platform).
The clip's state lives in Postgres, so the message is acknowledged once it has been looked at, whatever happened. A clip
that may not be posted yet (outside a window, cap reached) is not lost: every `sweep_every_s` the clips still waiting
at the publish stage are looked at again."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Protocol

import sqlalchemy as sa

from common import jobs
from common.streams import Consumer

log = logging.getLogger(__name__)
ALERT_REPEAT_S = 1800


class Publisher(Protocol):
    def process(self, job: jobs.Job) -> None: ...


class Throttle:
    """Wraps an alert so the same text goes out at most once every half hour."""

    def __init__(self, send: Callable[[str], object], *, clock: Callable[[], float] = time.monotonic) -> None:
        self._send, self._clock = send, clock
        self._last: dict[str, float] = {}

    def __call__(self, message: str) -> None:
        now = self._clock()
        if message in self._last and now - self._last[message] < ALERT_REPEAT_S:
            return
        self._last[message] = now
        self._send(message)


class PublisherService:
    def __init__(
        self,
        engine: sa.Engine,
        consumer: Consumer,
        publisher: Publisher,
        *,
        sweep_every_s: float = 60,
        clock: Callable[[], float] = time.monotonic,
        alert: Callable[[str], object] | None = None,
    ) -> None:
        self.engine, self.consumer, self.publisher = engine, consumer, publisher
        self.sweep_every_s, self.clock = sweep_every_s, clock
        self.alert = Throttle(alert or (lambda message: None), clock=clock)
        self._next_sweep = clock()

    def _process(self, job_id: uuid.UUID) -> None:
        with self.engine.connect() as conn:
            job = jobs.get(conn, job_id)
        if job is None or job["stage"] != "publish" or job["status"] != "queued":
            return
        try:
            self.publisher.process(job)
        except Exception as exc:
            log.exception("publishing job %s failed unexpectedly", job_id)
            self.alert(f"publishing job {job_id} hit an error: {type(exc).__name__}: {exc}")

    def step(self, block_ms: int | None = None) -> None:
        """Handle one message if there is one (waiting up to `block_ms`; None means do not wait), then sweep the waiting
        clips if the sweep is due."""
        delivery = self.consumer.read(block_ms)
        if delivery is not None:
            self._process(uuid.UUID(delivery.job_id))
            self.consumer.ack(delivery)
        if self.clock() >= self._next_sweep:
            self._next_sweep = self.clock() + self.sweep_every_s
            with self.engine.connect() as conn:
                waiting = (
                    conn.execute(
                        sa.text("SELECT id FROM jobs WHERE stage = 'publish' AND status = 'queued' ORDER BY created_at")
                    )
                    .scalars()
                    .all()
                )
            for job_id in waiting:
                self._process(job_id)
            housekeeping = getattr(self.publisher, "housekeeping", None)  # e.g. deleting files kept for download
            if housekeeping is not None:
                try:
                    housekeeping()
                except Exception as exc:
                    log.exception("housekeeping failed")
                    self.alert(f"housekeeping hit an error: {type(exc).__name__}: {exc}")
