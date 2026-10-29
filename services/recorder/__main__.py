"""Run the recorder: `python -m services.recorder`. Needs DATABASE_URL and REDIS_URL; MEDIA_ROOT (default ./media) and
FACTORY_HOST (default laptop, a key of hosts: in config/workers.yaml) are optional."""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from pathlib import Path

import redis
import yaml

from common import heartbeat
from common.alerts import make_alerter
from common.db import make_engine
from common.media import LocalStore
from common.rooms import from_config as rooms_from_config
from config_schema import ROOT
from services.recorder import record as rec
from services.recorder.service import RecorderService

log = logging.getLogger("recorder")
POLL_S = 10


def log_segment(stream_id: str, s: rec.Segment) -> None:
    log.info("segment %s #%d %.0f-%.0fs %s", stream_id, s.index, s.start_s, s.end_s, s.key)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    workers = yaml.safe_load((ROOT / "workers.yaml").read_text())
    host = workers["hosts"][os.environ.get("FACTORY_HOST", "laptop")]["services"]["recorder"]
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    engine = make_engine()
    heartbeat.start(engine, "recorder")
    service = RecorderService(
        engine,
        LocalStore(Path(os.environ.get("MEDIA_ROOT", "media"))),
        ROOT,
        f"{socket.gethostname()}-{os.getpid()}",
        max_concurrent=host["max_concurrent_recordings"],
        ttl_s=workers["lease"]["expires_s"],
        heartbeat_s=workers["lease"]["heartbeat_s"],
        on_segment=log_segment,
        alert=make_alerter("recorder", os.environ, rooms=rooms_from_config()),
        redis_client=redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True),
    )
    while not stop.is_set():
        try:
            for stream_id in service.poll():
                log.info("recording %s", stream_id)
        except Exception:
            log.exception("poll failed; trying again")
        stop.wait(POLL_S)
    service.shutdown()


if __name__ == "__main__":
    main()
