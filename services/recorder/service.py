"""The recorder service: record every live stream that has no worker, at most `max_concurrent` at a time."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import redis
import sqlalchemy as sa

import config_schema as cs
from common import leases, pauses, pipeline, sources
from common.media import MediaStore
from common.streams import publish
from services.live_watcher import detect
from services.recorder import record as rec
from services.recorder import run

log = logging.getLogger(__name__)
BASE_PAUSE_S, MAX_PAUSE_S, MAX_FAILURES = 10.0, 300.0, 30  # 30 failures in a row is about two and a half hours


class RecorderService:
    def __init__(
        self,
        engine: sa.Engine,
        store: MediaStore,
        config_dir: Path,
        worker: str,
        *,
        max_concurrent: int,
        ttl_s: float,
        heartbeat_s: float,
        on_segment: Callable[[str, rec.Segment], None],
        url_for: Callable[[str], str] = lambda video_id: f"https://www.youtube.com/watch?v={video_id}",
        record: Callable[..., str] = rec.record,
        still_live: Callable[[str], bool] = detect.is_live,
        redis_client: redis.Redis | None = None,
        alert: Callable[[str], object] | None = None,
        prefix: str = "",
        clock: Callable[[], float] = time.monotonic,
        max_failures: int = MAX_FAILURES,
    ) -> None:
        self.clock, self.max_failures = clock, max_failures
        self._failures: dict[uuid.UUID, int] = {}  # failed attempts in a row, with no segment in between
        self._retry_at: dict[uuid.UUID, float] = {}
        self.redis, self.prefix = redis_client, prefix
        self.alert = alert or (lambda message: None)
        self.engine, self.store, self.config_dir, self.worker = engine, store, config_dir, worker
        self.max_concurrent, self.ttl_s, self.heartbeat_s = max_concurrent, ttl_s, heartbeat_s
        self.on_segment, self.url_for, self.record, self.still_live = on_segment, url_for, record, still_live
        self.stop = threading.Event()
        self.running: dict[uuid.UUID, threading.Thread] = {}

    def _settings(self, conn: sa.Connection, stream_id: uuid.UUID) -> cs.Profile | None:
        """segment length and quality from the stream's source profile; None when it may not be recorded."""
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
            if (p.name, p.source.id) == (row.profile, row.source_key) and p.live.capture:
                return p
        return None

    def poll(self) -> list[uuid.UUID]:
        """Start recordings for streams that need a worker; returns the ones started."""
        self.running = {i: t for i, t in self.running.items() if t.is_alive()}
        started: list[uuid.UUID] = []
        with self.engine.connect() as conn:
            if pauses.intake_paused(conn, "live"):
                return started  # paused by hand: nothing new is started (recordings under way go on)
            waiting = leases.needing_a_worker(conn, self.ttl_s)
            for stream_id in waiting:
                if len(self.running) >= self.max_concurrent:
                    break
                if stream_id in self.running or self.clock() < self._retry_at.get(stream_id, 0.0):
                    continue  # running already, or pausing after a failure
                if (profile := self._settings(conn, stream_id)) is None:
                    continue
                thread = threading.Thread(target=self._record, args=(stream_id, profile), daemon=True)
                self.running[stream_id] = thread
                thread.start()
                started.append(stream_id)
        return started

    def _publish(self, job_id: uuid.UUID) -> None:
        if self.redis is not None:
            publish(self.redis, pipeline.stream_for("understand", self.prefix), str(job_id))

    def _record(self, stream_id: uuid.UUID, profile: cs.Profile) -> None:
        def passed_on(segment: rec.Segment) -> None:
            self._failures.pop(stream_id, None)  # it is making progress, so earlier failures do not count
            self.on_segment(str(stream_id), segment)

        try:
            outcome = run.run_stream(
                self.engine,
                self.store,
                stream_id,
                self.worker,
                segment_s=profile.live.segment_min * 60,
                quality=profile.live.quality,
                min_quality=profile.live.min_quality,
                ttl_s=self.ttl_s,
                heartbeat_s=self.heartbeat_s,
                on_segment=passed_on,
                url_for=self.url_for,
                record=self.record,
                still_live=self.still_live,
                stop=self.stop,
                profile=profile,
                publish_job=self._publish,
            )
            log.info("recording of %s finished: %s", stream_id, outcome)
        except Exception as exc:
            self._failed(stream_id, exc)

    def _failed(self, stream_id: uuid.UUID, exc: Exception) -> None:
        """Pause longer after each failure in a row, and give the stream up after too many: hammering a platform that
        refuses us (the stream went private, we are blocked) helps nobody."""
        failures = self._failures[stream_id] = self._failures.get(stream_id, 0) + 1
        if failures >= self.max_failures:
            with self.engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "UPDATE live_streams SET status = 'ended', ended_at = clock_timestamp(), "
                        "lease_worker = NULL, lease_heartbeat_at = NULL WHERE id = :i"
                    ),
                    {"i": stream_id},
                )
            log.error("recording of %s failed %d times in a row: giving up", stream_id, failures)
            self.alert(f"gave up recording stream {stream_id} after {failures} failures in a row; the last was: {exc}")
            return
        pause = min(BASE_PAUSE_S * 2 ** (failures - 1), MAX_PAUSE_S)
        self._retry_at[stream_id] = self.clock() + pause
        log.exception("recording of %s failed (%d in a row); trying again in %.0f s", stream_id, failures, pause)
        self.alert(f"the recording of stream {stream_id} failed and will be retried in {pause:.0f} s: {exc}")

    def join(self, timeout: float) -> None:
        for thread in list(self.running.values()):
            thread.join(timeout)

    def shutdown(self, timeout: float = 30) -> None:
        self.stop.set()
        self.join(timeout)
