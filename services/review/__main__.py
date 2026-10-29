"""Run the review service: `python -m services.review`. Needs DATABASE_URL, REDIS_URL, TELEGRAM_BOT_TOKEN and
TELEGRAM_ALLOWED_USERS (comma-separated numeric Telegram user ids); MEDIA_ROOT (default ./media) and FACTORY_HOST
(default laptop) are optional."""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path

import yaml

from common import heartbeat, jobs, pipeline, rules, spend
from common.alerts import make_alerter
from common.db import make_engine
from common.media import LocalStore
from common.r2 import R2
from common.rooms import from_config as rooms_from_config
from common.streams import Consumer, connect, consumer_name, publish
from common.telegram import Telegram, TelegramError
from common.worker import Worker
from config_schema import ROOT
from services.publisher import accounts as acc
from services.publisher.clipstore import ClipStore
from services.review.commands import MENU, Commands
from services.review.handler import make_handler
from services.review.listener import poll_forever
from services.understand import planner
from services.understand.__main__ import groq_limits

log = logging.getLogger("review")
STAGE = "review"
BLOCK_MS = 5000


def _clips(engine: object) -> ClipStore | None:
    """The full quality copy of each clip in R2, once its variables are in .env; without them approvals go as before."""
    host = acc.media_host(ROOT / "accounts.yaml")
    if host is None or not os.environ.get(host.bucket_env):
        log.warning("no R2 variables: clips go for review without a full quality link")
        return None
    r2 = R2.from_env(
        os.environ,
        account_id_env=host.account_id_env,
        bucket_env=host.bucket_env,
        access_key_env=host.access_key_env,
        secret_key_env=host.secret_key_env,
    )
    return ClipStore(engine, r2, host)  # type: ignore[arg-type]


def clips_default() -> int:
    """How many clips may be made in 24 hours when nothing was set from Telegram: `selection.clips_per_day` of the
    profile defaults."""
    defaults = yaml.safe_load((ROOT / "profiles" / "_defaults.yaml").read_text())
    return int((defaults.get("selection") or {}).get("clips_per_day", 0))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    workers = yaml.safe_load((ROOT / "workers.yaml").read_text())
    mine = workers["hosts"][os.environ.get("FACTORY_HOST", "laptop")]["services"][STAGE]
    retry = workers["retry"]
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    engine, client = make_engine(), connect(os.environ["REDIS_URL"], block_ms=BLOCK_MS)
    heartbeat.start(engine, "review")
    bot = Telegram(os.environ["TELEGRAM_BOT_TOKEN"])
    allowed = [int(x) for x in os.environ["TELEGRAM_ALLOWED_USERS"].replace(" ", "").split(",") if x]
    if not allowed:
        raise SystemExit("TELEGRAM_ALLOWED_USERS must list at least one numeric Telegram user id")
    rooms = rooms_from_config()
    clips = _clips(engine)
    limits = groq_limits(workers)
    health = workers.get("health") or {}
    commands = Commands(
        engine, limits, acc.load(ROOT / "accounts.yaml"),
        expected=health.get("expected"), stale_after_s=health.get("stale_after_s", 120),
        spend_limits=spend.limits_from(workers), planner_cfg=planner.config_from(workers),
        rule_ceilings=rules.ceilings_from(workers), clips_default=clips_default(),
    )  # fmt: skip
    try:
        bot.set_my_commands(MENU)  # the list Telegram shows when you type a slash
    except TelegramError as exc:
        log.warning("could not register the command menu: %s", exc)
    threading.Thread(
        target=poll_forever,
        kwargs={
            "engine": engine,
            "bot": Telegram(os.environ["TELEGRAM_BOT_TOKEN"]),
            "allowed": set(allowed),
            "publish_job": lambda job_id: publish(client, pipeline.stream_for("publish"), str(job_id)),
            "stop": stop,
            "commands": commands,
            "rooms": rooms,
            "requeue_job": lambda job_id, stage: publish(client, pipeline.stream_for(stage), str(job_id)),
            "clips": clips,
        },
        daemon=True,
    ).start()
    worker = Worker(
        engine=engine,
        r=client,
        stage=STAGE,
        consumer=Consumer(
            client,
            pipeline.stream_for(STAGE),
            STAGE,
            consumer_name(),
            visibility_timeout_s=mine.get("visibility_timeout_s", 300),  # making and sending the preview
            dlq=retry["dead_letter_stream"],
        ),
        handler=make_handler(
            engine, LocalStore(Path(os.environ.get("MEDIA_ROOT", "media"))), bot, allowed, rooms=rooms, clips=clips
        ),
        retry=jobs.Retry(max_attempts=retry["max_attempts"], backoff_s=tuple(retry["backoff_s"])),
        alert=make_alerter("review", os.environ, rooms=rooms_from_config()),
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
