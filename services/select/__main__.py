"""Run the select service: `python -m services.select`. Needs DATABASE_URL, REDIS_URL and the API key variable(s)
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
from common.streams import Consumer, connect, consumer_name, publish
from common.worker import Worker
from config_schema import ROOT, load_llm_prices
from services.select.handler import make_handler
from services.select.recap_step import RecapStep

log = logging.getLogger("select")
STAGE = "select"
BLOCK_MS = 5000


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    workers = yaml.safe_load((ROOT / "workers.yaml").read_text())
    mine = workers["hosts"][os.environ.get("FACTORY_HOST", "laptop")]["services"][STAGE]
    retry = workers["retry"]
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    engine, client = make_engine(), connect(os.environ["REDIS_URL"], block_ms=BLOCK_MS)
    heartbeat.start(engine, "select")
    prices, alert = (
        load_llm_prices(ROOT / "llm_prices.yaml"),
        make_alerter(STAGE, os.environ, rooms=rooms_from_config()),
    )

    gate = Gate(engine, alert, prices=prices, limits=spend.limits_from(workers))

    def to_render(job_id: object) -> object:
        return publish(client, pipeline.stream_for("render"), str(job_id))

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
            publish_job=to_render,
            recap=RecapStep(engine, make_client, prices, ROOT, publish_job=to_render, gate=gate),
            alert=alert,
            gate=gate,
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
