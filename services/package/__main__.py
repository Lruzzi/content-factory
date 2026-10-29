"""Run the package service: `python -m services.package`. Needs DATABASE_URL, REDIS_URL and the API key variable(s)
named by each profile's selection.llm_judge. FACTORY_HOST (default laptop) is optional."""

from __future__ import annotations

import logging
import os
import signal
import threading

import yaml

from common import heartbeat, jobs, pipeline, spend
from common.alerts import make_alerter
from common.db import make_engine
from common.llm.factory import make_client
from common.llm.gate import Gate
from common.rooms import from_config as rooms_from_config
from common.streams import Consumer, connect, consumer_name
from common.worker import Worker
from config_schema import ROOT, load_llm_prices
from services.package.handler import make_handler

log = logging.getLogger("package")
STAGE = "package"
BLOCK_MS = 5000


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    workers = yaml.safe_load((ROOT / "workers.yaml").read_text())
    mine = workers["hosts"][os.environ.get("FACTORY_HOST", "laptop")]["services"][STAGE]
    retry = workers["retry"]
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    alert = make_alerter("package", os.environ, rooms=rooms_from_config())
    engine, client = make_engine(), connect(os.environ["REDIS_URL"], block_ms=BLOCK_MS)
    heartbeat.start(engine, "package")
    prices = load_llm_prices(ROOT / "llm_prices.yaml")
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
            make_client,
            prices,
            ROOT,
            gate=Gate(engine, alert, prices=prices, limits=spend.limits_from(workers)),
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
