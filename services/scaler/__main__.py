"""Run the scaler: `python -m services.scaler`. Needs DATABASE_URL and REDIS_URL, and the Telegram variables for
alerts. Settings come from config/workers.yaml (reconciler, retry, broker)."""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path

import yaml

from common import heartbeat, jobs
from common.alerts import make_alerter, make_poster
from common.db import make_engine
from common.media import LocalStore
from common.rooms import from_config as rooms_from_config
from common.streams import connect
from config_schema import ROOT, validate_workers
from services.publisher import accounts as acc
from services.review.commands import Commands
from services.scaler import retention
from services.scaler.loop import run
from services.scaler.monitor import Monitor
from services.understand.__main__ import groq_limits

log = logging.getLogger("scaler")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    validate_workers(ROOT / "workers.yaml")  # a clear message for a missing or wrong setting, not a KeyError
    workers = yaml.safe_load((ROOT / "workers.yaml").read_text())
    sweep, retry, broker = workers["reconciler"], workers["retry"], workers["broker"]
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("reconciling every %s s, cleaning up every %s min", sweep["every_s"], workers["retention"]["every_min"])
    rooms = rooms_from_config()
    engine, alert = make_engine(), make_alerter("scaler", os.environ, rooms=rooms)
    heartbeat.start(engine, "scaler")
    health = workers.get("health")
    if health:  # the watchdog: who went quiet, who is back, and a digest for the monitoring room
        commands = Commands(
            engine, groq_limits(workers), acc.load(ROOT / "accounts.yaml"),
            expected=health["expected"], stale_after_s=health["stale_after_s"],
        )  # fmt: skip
        monitor = Monitor(
            engine, health["expected"], stale_after_s=health["stale_after_s"], alert=alert,
            post=make_poster(os.environ, room="monitoring", rooms=rooms),
            digest_text=lambda: f"Digest\n{commands.status()}\n\n{commands.posts()}",
            digest_every_s=health["digest_every_h"] * 3600,
        )  # fmt: skip
        threading.Thread(target=monitor.run, args=(stop,), daemon=True).start()
    keep = workers["retention"]
    threading.Thread(
        target=retention.run,
        args=(
            engine,
            LocalStore(Path(os.environ.get("MEDIA_ROOT", "media"))),
            retention.Policy(keep["published_keep_h"], keep["failed_keep_days"], keep["approved_max_hold_days"]),
        ),
        kwargs={"every_s": keep["every_min"] * 60, "stop": stop, "alert": alert},
        daemon=True,
    ).start()
    run(
        engine,
        connect(os.environ["REDIS_URL"]),
        every_s=sweep["every_s"],
        stalled_after_min=sweep["stalled_after_min"],
        running_after_min=sweep["running_after_min"],
        retry=jobs.Retry(max_attempts=retry["max_attempts"], backoff_s=tuple(retry["backoff_s"])),
        dlq=retry["dead_letter_stream"],
        alert=alert,
        maxlen=broker["stream_maxlen"],
        dlq_maxlen=broker["dead_letter_maxlen"],
        stop=stop,
    )


if __name__ == "__main__":
    main()
