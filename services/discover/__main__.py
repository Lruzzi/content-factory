"""Run discover: `python -m services.discover`. Every few minutes it looks for ended streams and uploads of the sources
that are not live and queues them for download. Needs DATABASE_URL and REDIS_URL."""

from __future__ import annotations

import logging
import os
import signal
import threading
import uuid

import yaml

from common import heartbeat, pipeline, sources
from common.alerts import make_alerter
from common.db import make_engine
from common.rooms import from_config as rooms_from_config
from common.streams import connect, publish
from config_schema import ROOT
from services.discover import tick as discover
from services.understand.__main__ import groq_limits

log = logging.getLogger("discover")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    engine, client = make_engine(), connect(os.environ["REDIS_URL"])
    heartbeat.start(engine, "discover")
    alert = make_alerter("discover", os.environ, rooms=rooms_from_config())
    limits = groq_limits(yaml.safe_load((ROOT / "workers.yaml").read_text()))

    def to_download(job_id: uuid.UUID) -> None:
        publish(client, pipeline.stream_for("download"), str(job_id))

    told: set[str] = set()  # a problem that lasts is reported once, not every round
    wait_min = 30
    while not stop.is_set():
        try:
            with engine.begin() as conn:
                sources.seed_from_yaml(conn, ROOT)
            result = discover.tick(engine, ROOT, limits=limits, publish_job=to_download)
            log.info("queued=%s skipped=%d problems=%d", result.queued, len(result.skipped), len(result.problems))
            for line in result.skipped:
                log.info("skipped %s", line)
            for problem in result.problems:
                log.warning("problem: %s", problem)
                if problem not in told:
                    alert(problem)
            told = set(result.problems)
            with engine.connect() as conn:
                profiles, _ = sources.load_profiles(conn, ROOT)
            wait_min = min((p.discover.poll_every_min for p in profiles if p.enabled and p.source.enabled), default=30)
        except Exception:
            log.exception("round failed; trying again next time")
        stop.wait(wait_min * 60)


if __name__ == "__main__":
    main()
