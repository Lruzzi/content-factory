"""Run the understand service: `python -m services.understand`. Needs DATABASE_URL, REDIS_URL and GROQ_API_KEY;
MEDIA_ROOT (default ./media) and FACTORY_HOST (default laptop) are optional."""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path

import yaml

from common import heartbeat, jobs, pipeline, spend
from common.alerts import make_alerter
from common.db import make_engine
from common.media import LocalStore
from common.rooms import from_config as rooms_from_config
from common.streams import Consumer, connect, consumer_name
from common.worker import Worker
from config_schema import ROOT
from services.understand import planner
from services.understand.budget import Limits
from services.understand.handler import make_handler
from services.understand.transcribe import GroqTranscriber, OpenRouterTranscriber

log = logging.getLogger("understand")
STAGE = "understand"
BLOCK_MS = 5000


def groq_limits(workers: dict) -> Limits:
    groq = workers["rate_limits"]["groq"]
    return Limits(
        groq["audio_seconds_per_hour"], groq["audio_seconds_per_day"], groq["alert_at"], groq.get("stop_at", 1.0)
    )


def _planning(workers: dict):
    """The hours planner as the handler wants it, or None when workers.yaml has none or it is switched off."""
    cfg = planner.config_from(workers)
    if cfg is None or not cfg.enabled:
        return None
    groq, spend_limits = groq_limits(workers), spend.limits_from(workers)
    return lambda conn, source_id, needed_s: planner.check(conn, cfg, groq, spend_limits, source_id, needed_s)


def _fallback() -> OpenRouterTranscriber | None:
    """OpenRouter's Whisper for the overflow, when its key is set."""
    key = os.environ.get("OPEN_ROUTER_KEY")
    return OpenRouterTranscriber(key) if key else None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    workers = yaml.safe_load((ROOT / "workers.yaml").read_text())
    mine = workers["hosts"][os.environ.get("FACTORY_HOST", "laptop")]["services"][STAGE]
    retry = workers["retry"]
    alert = make_alerter(STAGE, os.environ, rooms=rooms_from_config())
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    engine, client = make_engine(), connect(os.environ["REDIS_URL"], block_ms=BLOCK_MS)
    heartbeat.start(engine, "understand")
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
        handler=make_handler(
            engine,
            LocalStore(Path(os.environ.get("MEDIA_ROOT", "media"))),
            GroqTranscriber(os.environ["GROQ_API_KEY"]),
            limits=groq_limits(workers),
            alert=alert,
            plan=_planning(workers),
            fallback=_fallback(),
            spend_limits=spend.limits_from(workers),
        ),
        retry=jobs.Retry(max_attempts=retry["max_attempts"], backoff_s=tuple(retry["backoff_s"])),
        alert=alert,
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
