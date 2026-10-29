"""Record one `live_streams` row: take the lease, keep it alive while recording, store each finished segment and
settle the row afterwards. A recording that stops, drops or fails leaves the stream `live`, so the recorder service
starts it again where it left off; only a stream that is really over is marked `ended`."""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable

import sqlalchemy as sa

import config_schema as cs
from common import jobs, leases
from common.media import MediaStore
from services.recorder import record as rec

log = logging.getLogger(__name__)


def run_stream(
    engine: sa.Engine,
    store: MediaStore,
    stream_id: uuid.UUID,
    worker: str,
    *,
    segment_s: int,
    quality: str,
    ttl_s: float,
    heartbeat_s: float,
    on_segment: Callable[[rec.Segment], None],
    still_live: Callable[[str], bool] = lambda video_id: False,
    url_for: Callable[[str], str] = lambda video_id: f"https://www.youtube.com/watch?v={video_id}",
    record: Callable[..., str] = rec.record,
    stop: threading.Event | None = None,
    profile: cs.Profile | None = None,
    publish_job: Callable[[uuid.UUID], None] | None = None,
    min_quality: str | None = None,
) -> str:
    """Returns "ended", "stopped", "dropped" (the recording ended but the stream is still live), "lost"
    (another worker took the lease) or "busy" (someone else is recording). `still_live` is asked whenever a
    recording ends or fails; if it cannot tell (raises), the stream is assumed live. With a `profile`, each new
    segment also becomes a job at the understand stage, made in the same transaction as the segment; `publish_job`
    then sends its id to the queue, and if that fails the reconciler does it later."""
    with engine.begin() as conn:
        if not leases.acquire(conn, stream_id, worker, ttl_s):
            return "busy"
        video_id, source_id = conn.execute(
            sa.text("SELECT video_id, source_id FROM live_streams WHERE id = :i"), {"i": stream_id}
        ).one()
        next_index, next_start_s = conn.execute(
            sa.text("SELECT coalesce(max(idx) + 1, 0), coalesce(max(end_s), 0) FROM segments WHERE stream_id = :i"),
            {"i": stream_id},
        ).one()
    stop = stop or threading.Event()
    lost, finished = threading.Event(), threading.Event()

    def beat() -> None:
        while not finished.wait(heartbeat_s):
            try:
                with engine.begin() as conn:
                    holding = leases.heartbeat(conn, stream_id, worker)
            except sa.exc.SQLAlchemyError:
                continue  # a database blip: try again, the lease only expires after ttl_s
            if not holding:
                lost.set()
                stop.set()
                return

    def store_and_pass_on(segment: rec.Segment) -> None:
        with engine.begin() as conn:
            new = conn.execute(
                sa.text(
                    "INSERT INTO segments (stream_id, idx, key, start_s, end_s) VALUES (:s, :i, :k, :a, :b) "
                    "ON CONFLICT (stream_id, idx) DO NOTHING RETURNING id"
                ),
                {"s": stream_id, "i": segment.index, "k": segment.key, "a": segment.start_s, "b": segment.end_s},
            ).first()
            job_id = None
            if new is not None and profile is not None:
                job_id = jobs.create_job(
                    conn,
                    profile,
                    stage="understand",
                    source_id=source_id,
                    media_key=segment.key,
                    data={
                        "live": True, "stream_id": str(stream_id), "video_id": video_id,
                        "segment": segment.index, "start_s": segment.start_s, "end_s": segment.end_s,
                    },
                )  # fmt: skip
        if job_id is not None and publish_job is not None:
            try:
                publish_job(job_id)
            except Exception:
                log.exception("could not queue job %s; it stays queued in Postgres for the reconciler", job_id)
        on_segment(segment)

    def is_over() -> bool:
        try:
            return not still_live(video_id)
        except Exception:
            return False

    heart = threading.Thread(target=beat, daemon=True)
    heart.start()
    outcome = "stopped"
    try:
        outcome = record(
            url_for(video_id),
            store,
            str(stream_id),
            segment_s=segment_s,
            on_segment=store_and_pass_on,
            quality=quality,
            min_quality=min_quality,
            stop=stop,
            start_index=int(next_index),
            start_s=float(next_start_s),
        )
        if outcome == "ended" and not lost.is_set():
            outcome = "ended" if is_over() else "dropped"
    except rec.RecorderError:
        outcome = "ended" if not lost.is_set() and is_over() else "failed"
        raise
    finally:
        finished.set()
        heart.join()
        if not lost.is_set():
            with engine.begin() as conn:
                if outcome == "ended":
                    conn.execute(
                        sa.text(
                            "UPDATE live_streams SET status = 'ended', ended_at = clock_timestamp() "
                            "WHERE id = :i AND lease_worker = :w"
                        ),
                        {"i": stream_id, "w": worker},
                    )
                leases.release(conn, stream_id, worker)
    return "lost" if lost.is_set() else outcome
