"""Listen to the chat of one live stream: hold the chat lease, count the messages per bucket, save the counts every few
seconds, and stop when the stream is over, the chat is closed or the lease is lost."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable

import sqlalchemy as sa

from common import leases
from services.chat_listener import counts
from services.chat_listener.reader import ChatReader

log = logging.getLogger(__name__)


def listen_to(
    engine: sa.Engine,
    stream_id: uuid.UUID,
    worker: str,
    *,
    make_reader: Callable[[str], ChatReader],
    bucket_s: int,
    ttl_s: float,
    heartbeat_s: float,
    poll_s: float = 3.0,
    stop: threading.Event | None = None,
    now: Callable[[], float] = time.time,
) -> str:
    """Returns "ended" (the stream is over or its chat closed), "stopped", "lost" (another worker took the lease) or
    "busy" (someone else is listening)."""
    stop = stop or threading.Event()
    with engine.begin() as conn:
        if not leases.acquire(conn, stream_id, worker, ttl_s, kind="chat"):
            return "busy"
        video_id = conn.execute(
            sa.text("SELECT video_id FROM live_streams WHERE id = :i"), {"i": stream_id}
        ).scalar_one()
    pending: Counter[int] = Counter()
    last_beat = now()
    outcome = "stopped"
    reader: ChatReader | None = None
    try:
        reader = make_reader(video_id)
        while not stop.is_set():
            pending.update(counts.bucket_of(t, bucket_s) for t in reader.poll())
            if now() - last_beat >= heartbeat_s or not reader.alive():
                last_beat = now()
                with engine.begin() as conn:
                    if pending:
                        counts.add(conn, stream_id, pending)
                        pending.clear()
                    if not leases.heartbeat(conn, stream_id, worker, kind="chat"):
                        return "lost"
                    live = conn.execute(
                        sa.text("SELECT status = 'live' FROM live_streams WHERE id = :i"), {"i": stream_id}
                    ).scalar_one()
                if not live or not reader.alive():
                    outcome = "ended"
                    break
            stop.wait(poll_s)
    finally:
        if reader is not None:
            reader.close()
        with engine.begin() as conn:
            if pending:
                counts.add(conn, stream_id, pending)
            leases.release(conn, stream_id, worker, kind="chat")
    return outcome
