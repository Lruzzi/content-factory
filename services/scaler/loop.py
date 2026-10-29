"""Run the reconciler on a timer. Postgres is the source of truth and Redis only carries ids, so whatever the queue
loses (a message dropped, a worker that died, a retry that came due) is rebuilt from the database here. A sweep that
fails (the queue is down, for instance) is reported and tried again at the next tick; it never ends the loop. Two copies
may run: the sweep takes a database lock, so only one of them does the work."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

import redis
import sqlalchemy as sa

from common import jobs, reconciler
from common.streams import DEFAULT_DLQ_MAXLEN, DEFAULT_MAXLEN

log = logging.getLogger(__name__)


def run(
    engine: sa.Engine,
    r: redis.Redis,
    *,
    every_s: float,
    stalled_after_min: float,
    running_after_min: float,
    retry: jobs.Retry,
    stop: threading.Event,
    prefix: str = "",
    dlq: str = "dlq",
    alert: Callable[[str], object] | None = None,
    maxlen: int = DEFAULT_MAXLEN,
    dlq_maxlen: int = DEFAULT_DLQ_MAXLEN,
    sweep: Callable[..., Any] = reconciler.sweep,
) -> None:
    while not stop.is_set():
        try:
            swept = sweep(
                engine, r, stalled_after_min=stalled_after_min, running_after_min=running_after_min, retry=retry,
                prefix=prefix, dlq=dlq, alert=alert, maxlen=maxlen, dlq_maxlen=dlq_maxlen,
            )  # fmt: skip
            if swept and (swept.retried or swept.republished or swept.timed_out or swept.dead):
                log.info("reconciled: %s", swept)
        except Exception as exc:
            log.exception("a reconciler sweep failed; trying again at the next tick")
            if alert:
                alert(f"the reconciler could not run: {type(exc).__name__}: {exc}")
        stop.wait(every_s)
