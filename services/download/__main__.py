"""Run the download service: `python -m services.download`. Needs DATABASE_URL and REDIS_URL; MEDIA_ROOT (default
./media) and FACTORY_HOST (default laptop) are optional."""

from __future__ import annotations

import logging
import os
import signal
import threading
import uuid
from pathlib import Path

import yaml

from common import heartbeat, jobs, pipeline
from common.alerts import make_alerter
from common.db import make_engine
from common.media import LocalStore
from common.rooms import from_config as rooms_from_config
from common.streams import Consumer, connect, consumer_name, publish
from common.worker import Worker
from config_schema import ROOT
from services.download.handler import make_handler

log = logging.getLogger("download")
STAGE = "download"
BLOCK_MS = 5000


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    workers = yaml.safe_load((ROOT / "workers.yaml").read_text())
    mine = workers["hosts"][os.environ.get("FACTORY_HOST", "laptop")]["services"][STAGE]
    retry = workers["retry"]
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    media_root = Path(os.environ.get("MEDIA_ROOT", "media"))
    scratch = media_root / "download-tmp"  # a downloaded hour is large: keep it on the volume, not in memory
    scratch.mkdir(parents=True, exist_ok=True)
    engine, client = make_engine(), connect(os.environ["REDIS_URL"], block_ms=BLOCK_MS)
    heartbeat.start(engine, "download")

    def to_understand(job_id: uuid.UUID) -> None:
        publish(client, pipeline.stream_for("understand"), str(job_id))

    worker = Worker(
        engine=engine,
        r=client,
        stage=STAGE,
        consumer=Consumer(
            client,
            pipeline.stream_for(STAGE),
            STAGE,
            consumer_name(),
            visibility_timeout_s=mine["visibility_timeout_s"],
            dlq=retry["dead_letter_stream"],
        ),
        handler=make_handler(engine, LocalStore(media_root), publish_job=to_understand, scratch=scratch),
        retry=jobs.Retry(max_attempts=retry["max_attempts"], backoff_s=tuple(retry["backoff_s"])),
        alert=make_alerter("download", os.environ, rooms=rooms_from_config()),
    )
    log.info("waiting for jobs")
    while not stop.is_set():
        try:
            worker.run_once(block_ms=BLOCK_MS)
        except Exception:
            log.exception("worker loop failed; trying again")
            stop.wait(5)


if __name__ == "__main__":
    main()
