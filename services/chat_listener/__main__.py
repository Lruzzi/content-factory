"""Run the chat listener: `python -m services.chat_listener`. Needs DATABASE_URL; FACTORY_HOST (default laptop, a key of
hosts: in config/workers.yaml) is optional."""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading

import yaml

from common import heartbeat
from common.db import make_engine
from config_schema import ROOT
from services.chat_listener.service import ChatService

log = logging.getLogger("chat_listener")
POLL_S = 10


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "httpcore", "h2", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # one INFO line for every poll of the chat, URL included
    workers = yaml.safe_load((ROOT / "workers.yaml").read_text())
    host = workers["hosts"][os.environ.get("FACTORY_HOST", "laptop")]["services"]["chat-listener"]
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    engine = make_engine()
    heartbeat.start(engine, "chat-listener")
    service = ChatService(
        engine, ROOT, f"{socket.gethostname()}-{os.getpid()}", max_concurrent=host["max_concurrent"],
        ttl_s=workers["lease"]["expires_s"], heartbeat_s=workers["lease"]["heartbeat_s"],
    )  # fmt: skip
    while not stop.is_set():
        try:
            for stream_id in service.poll():
                log.info("listening to the chat of %s", stream_id)
        except Exception:
            log.exception("poll failed; trying again")
        stop.wait(POLL_S)
    service.shutdown()


if __name__ == "__main__":
    main()
