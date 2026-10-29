"""The watchdog: a service that goes quiet is reported once (to the alerts room), reported once more when it is back (to
the monitoring room), and a digest of how things stand goes to the monitoring room now and then. It trusts only the
heartbeats in Postgres. It cannot watch itself: Docker's restart policy covers the scaler."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import sqlalchemy as sa

from common import heartbeat

log = logging.getLogger(__name__)


class Monitor:
    def __init__(
        self,
        engine: sa.Engine,
        expected: list[str],
        *,
        stale_after_s: float,
        alert: Callable[[str], object],
        post: Callable[[str], object],
        digest_text: Callable[[], str],
        digest_every_s: float,
        grace_s: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine, self.expected, self.stale_after_s = engine, expected, stale_after_s
        self._alert, self._post, self._digest_text = alert, post, digest_text
        self._digest_every_s, self._grace_s, self._clock = digest_every_s, grace_s, clock
        self._started = clock()
        self._next_digest = self._started + digest_every_s
        self._down: set[str] = set()

    def _say(self, send: Callable[[str], object], message: str) -> None:
        try:
            send(message)
        except Exception:
            log.exception("could not send a monitoring message")

    def check(self) -> None:
        """Compare the heartbeats with what is expected, say what changed, and send the digest when it is due."""
        with self.engine.connect() as conn:
            found = heartbeat.states(conn, self.expected, stale_after_s=self.stale_after_s)
        waited = self._clock() - self._started
        for state in found:
            if state.up is False or (state.up is None and waited > self._grace_s):
                if state.name not in self._down:
                    self._down.add(state.name)
                    since = (
                        f"has not reported for {(state.age_s or 0) / 60:.0f} min (stopped or stuck?)"
                        if state.age_s is not None
                        else "has never reported since the watchdog started"
                    )
                    self._say(self._alert, f"{state.name} {since}")
            elif state.up and state.name in self._down:
                self._down.discard(state.name)
                self._say(self._post, f"✅ {state.name} is back")
        if self._clock() >= self._next_digest:
            self._next_digest = self._clock() + self._digest_every_s
            self._say(self._post, self._digest_text())

    def run(self, stop: threading.Event, every_s: float = 60) -> None:
        while not stop.is_set():
            try:
                self.check()
            except Exception:
                log.exception("a health check failed; trying again")
            stop.wait(every_s)
