"""Run the live-watcher: `python -m services.live_watcher`. Needs DATABASE_URL."""

from __future__ import annotations

import logging
import os
import signal
import threading

from common import heartbeat, sources
from common.alerts import make_alerter
from common.db import make_engine
from common.rooms import from_config as rooms_from_config
from config_schema import ROOT
from services.live_watcher import watcher

log = logging.getLogger("live_watcher")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    engine = make_engine()
    heartbeat.start(engine, "live-watcher")
    with engine.begin() as conn:
        log.info("seeded %d sources from YAML", sources.seed_from_yaml(conn, ROOT))
    alert = make_alerter("live-watcher", os.environ, rooms=rooms_from_config())
    wait_min = 4
    while not stop.is_set():
        try:
            with engine.begin() as conn:
                result = watcher.tick(conn, ROOT)
            wait_min = result.wait_min
            log.info("checked: started=%s skipped=%s", result.started, result.skipped)
            for problem in result.problems:
                log.warning("problem: %s", problem)
                alert(problem)
        except Exception:
            log.exception("check failed; trying again next time")
        stop.wait(wait_min * 60)


if __name__ == "__main__":
    main()
