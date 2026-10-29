"""Feed a public recording through the pipeline as if it had been captured live (a test tool).

The recording is cut into the same numbered .ts segments a live capture makes, stored as an already-ended stream, and
each segment becomes an understand job. Its source is always `clip_policy: blocked` with a small `top_k`, so nothing it
makes can be posted. `forget` removes everything a replay (or any stream) left: rows, jobs and files."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa

import config_schema as cs
from common import sources
from common.media import MediaStore
from services.download import ingest, ytdlp
from services.download.ytdlp import download_command  # noqa: F401  (the tests and the tool read it from here)

log = logging.getLogger(__name__)
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
LINK = re.compile(
    r"https://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|live/)|youtu\.be/)([A-Za-z0-9_-]{11})(?:[&?#/].*)?"
)


class ReplayError(RuntimeError):
    pass


@dataclass(frozen=True)
class Replayed:
    stream_id: uuid.UUID
    segments: int
    jobs: list[uuid.UUID]


def video_id_of(link: str) -> str:
    """The id inside a link to one YouTube video; anything else (a channel, another site, an option) is refused."""
    match = LINK.fullmatch(link.strip())
    if match is None:
        raise ValueError(f"not a link to a single YouTube video: {link[:60]!r}")
    return match.group(1)


def download(video_id: str, directory: Path, *, start_s: int, length_s: int, height: int = 1080) -> Path:
    return ytdlp.fetch(video_id, directory, start_s=start_s, length_s=length_s, height=height)


def _profile(
    conn: sa.Connection, config_dir: Path, like: str, top_k: int, layout: str | None, name: str | None
) -> tuple[cs.Profile, uuid.UUID]:
    profiles, _ = sources.load_profiles(conn, config_dir)
    base = next((p for p in profiles if p.source.id == like), None)
    if base is None:
        raise ReplayError(f"no source named {like!r} to copy the settings from")
    spec = base.model_dump(mode="json")
    spec["source"]["clip_policy"] = "blocked"
    if name:
        spec["source"]["name"] = name  # shown in the credit: the copied source's own name would be wrong
    spec["selection"]["top_k"] = top_k
    spec["selection"]["mode"] = "transcript"  # a replay has no chat, heatmap or loudness: only the words
    spec["selection"]["clips_per_day"] = (
        0  # a replay is a test: it neither uses up the day's clips nor is stopped by them
    )
    spec["signals"]["transcript"]["mode"] = "full"
    spec["compilation"]["enabled"] = False  # recaps are tested on their own; a replay makes plain clips
    if layout:
        spec["layout"] = {"type": layout}  # for a source whose own layout needs more than the recording can give
    profile = cs.Profile.model_validate(spec)
    if profile.upload_accounts():
        raise ReplayError("a replay profile must not have anywhere to upload to")
    source_id = conn.execute(sa.text("SELECT id FROM sources WHERE source_key = :k"), {"k": like}).scalar_one()
    return profile, source_id


def replay(
    engine: sa.Engine,
    store: MediaStore,
    video: Path,
    *,
    video_id: str,
    like: str,
    config_dir: Path,
    top_k: int = 1,
    layout: str | None = None,
    name: str | None = None,
    segment_s: int = 600,
    publish_job: Callable[[uuid.UUID], object] | None = None,
    workdir: Path | None = None,
) -> Replayed:
    """Store a recording as an ended stream of segments, each with an understand job. Stores nothing on an error."""
    with engine.connect() as conn:
        profile, source_id = _profile(conn, config_dir, like, top_k, layout, name)
        if conn.execute(sa.text("SELECT 1 FROM live_streams WHERE video_id = :v"), {"v": video_id}).first():
            raise ReplayError(f"{video_id} is already stored: forget it first")
    stream_id = uuid.uuid4()

    def open_stream(conn: sa.Connection) -> None:
        conn.execute(
            sa.text(
                "INSERT INTO live_streams (id, source_id, video_id, status, ended_at) "
                "VALUES (:i, :s, :v, 'ended', clock_timestamp())"
            ),
            {"i": stream_id, "s": source_id, "v": video_id},
        )

    job_ids = ingest.ingest(
        engine, store, video, stream_id=stream_id, video_id=video_id, profile=profile, source_id=source_id,
        segment_s=segment_s, workdir=workdir, before=open_stream,
    )  # fmt: skip
    for job_id in job_ids:
        if publish_job is not None:
            try:
                publish_job(job_id)
            except Exception:
                log.exception("could not queue job %s; it stays queued in Postgres for the reconciler", job_id)
    return Replayed(stream_id, len(job_ids), job_ids)


def forget(engine: sa.Engine, store: MediaStore, video_id: str) -> int:
    """Remove a stream's rows, its jobs (segments and recaps made from it) and the files they hold. Returns how many
    streams were removed (0 or 1)."""
    with engine.begin() as conn:
        row = conn.execute(sa.text("SELECT id FROM live_streams WHERE video_id = :v"), {"v": video_id}).first()
        if row is None:
            return 0
        stream_id = row[0]
        keys: set[str] = set()
        keys.update(conn.execute(sa.text("SELECT key FROM segments WHERE stream_id = :s"), {"s": stream_id}).scalars())
        params: dict[str, Any] = {"s": str(stream_id), "v": video_id}
        # a job belongs to the stream if it names it, is a recap of its video, or was made from one of its segments
        keys.update(
            conn.execute(
                sa.text(
                    "SELECT data->'render'->'output'->>'key' FROM jobs "
                    "WHERE (data->>'stream_id' = :s OR data->'clip'->>'video_id' = :v "
                    "OR media_key IN (SELECT key FROM segments WHERE stream_id = CAST(:s AS uuid))) "
                    "AND data->'render'->'output'->>'key' IS NOT NULL"
                ),
                params,
            ).scalars()
        )
        conn.execute(
            sa.text(
                "DELETE FROM jobs WHERE data->>'stream_id' = :s OR data->'clip'->>'video_id' = :v "
                "OR media_key IN (SELECT key FROM segments WHERE stream_id = CAST(:s AS uuid))"
            ),
            params,
        )
        conn.execute(sa.text("DELETE FROM live_streams WHERE id = :s"), {"s": stream_id})  # rows cascade
    for key in keys:
        try:
            store.delete(key)
        except Exception:
            log.warning("could not delete %s", key)
    return 1
