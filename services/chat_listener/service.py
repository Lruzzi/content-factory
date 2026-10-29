"""The chat listener service: listen to the chat of every live stream that has none, at most `max_concurrent` at
once."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import sqlalchemy as sa

import config_schema as cs
from common import leases, pauses, sources
from services.chat_listener import run
from services.chat_listener.reader import ChatReader, PytchatReader

log = logging.getLogger(__name__)
RETRY_S = 300.0  # a chat that could not be read (closed, members only, YouTube refused) is tried again this much later


class ChatService:
    def __init__(
        self,
        engine: sa.Engine,
        config_dir: Path,
        worker: str,
        *,
        max_concurrent: int,
        ttl_s: float,
        heartbeat_s: float,
        make_reader: Callable[[str], ChatReader] = PytchatReader,
        clock: Callable[[], float] = time.monotonic,
        poll_s: float = 3.0,
    ) -> None:
        self.engine, self.config_dir, self.worker = engine, config_dir, worker
        self.max_concurrent, self.ttl_s, self.heartbeat_s = max_concurrent, ttl_s, heartbeat_s
        self.make_reader, self.clock, self.poll_s = make_reader, clock, poll_s
        self.stop = threading.Event()
        self.running: dict[uuid.UUID, threading.Thread] = {}
        self._retry_at: dict[uuid.UUID, float] = {}

    def _profile(self, conn: sa.Connection, stream_id: uuid.UUID) -> cs.Profile | None:
        """The profile of the stream's source, or None when its chat is not to be read (off, paused, not recorded)."""
        row = conn.execute(
            sa.text(
                "SELECT s.profile, s.source_key, s.paused_at FROM live_streams l "
                "JOIN sources s ON s.id = l.source_id WHERE l.id = :i"
            ),
            {"i": stream_id},
        ).first()
        if row is None or row.paused_at is not None:
            return None
        profiles, _ = sources.load_profiles(conn, self.config_dir)
        for p in profiles:
            if (p.name, p.source.id) == (row.profile, row.source_key) and p.live.capture and p.signals.chat.enabled:
                return p
        return None

    def poll(self) -> list[uuid.UUID]:
        """Start listening to streams that need it; returns the ones started."""
        self.running = {i: t for i, t in self.running.items() if t.is_alive()}
        started: list[uuid.UUID] = []
        with self.engine.connect() as conn:
            if pauses.intake_paused(conn, "live"):
                return started
            for stream_id in leases.needing_a_worker(conn, self.ttl_s, kind="chat"):
                if len(self.running) >= self.max_concurrent:
                    break
                if stream_id in self.running or self.clock() < self._retry_at.get(stream_id, 0.0):
                    continue
                if (profile := self._profile(conn, stream_id)) is None:
                    continue
                thread = threading.Thread(target=self._listen, args=(stream_id, profile), daemon=True)
                self.running[stream_id] = thread
                thread.start()
                started.append(stream_id)
        return started

    def _listen(self, stream_id: uuid.UUID, profile: cs.Profile) -> None:
        window_s = profile.signals.chat.window_s or 10
        try:
            outcome = run.listen_to(
                self.engine, stream_id, self.worker, make_reader=self.make_reader, bucket_s=window_s,
                ttl_s=self.ttl_s, heartbeat_s=self.heartbeat_s, poll_s=self.poll_s, stop=self.stop,
            )  # fmt: skip
            log.info("listening to the chat of %s finished: %s", stream_id, outcome)
        except Exception as exc:  # a chat that cannot be read costs a signal, never the recording
            self._retry_at[stream_id] = self.clock() + RETRY_S
            log.warning(
                "could not read the chat of %s (%s); trying again in %.0f s", stream_id, type(exc).__name__, RETRY_S
            )

    def join(self, timeout: float) -> None:
        for thread in list(self.running.values()):
            thread.join(timeout)

    def shutdown(self, timeout: float = 30) -> None:
        self.stop.set()
        self.join(timeout)
