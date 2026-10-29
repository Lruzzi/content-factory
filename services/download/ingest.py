"""Turn a video file into the numbered segments a live capture makes: stored files, `segments` rows and one understand
job per segment, so everything after this point treats it exactly like a recording."""

from __future__ import annotations

import math
import shutil
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

import sqlalchemy as sa

import config_schema as cs
from common import jobs
from common.media import MediaStore
from services.download.proc import DownloadError, binary, run
from services.render import ffmpeg as ff

CUT_TIMEOUT_S = 4 * 3600


def _command(video: Path, directory: Path, segment_s: int, *, copy: bool) -> list[str]:
    codec = (
        ["-c", "copy"]
        if copy
        else [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-force_key_frames",
            f"expr:gte(t,n_forced*{segment_s})",
            "-c:a",
            "aac",
        ]  # fmt: skip
    )
    return [
        binary("ffmpeg"), "-v", "error", "-nostdin", "-protocol_whitelist", "file", "-i", str(video), *codec,
        "-f", "segment", "-segment_time", str(segment_s), "-reset_timestamps", "1", "-segment_format", "mpegts",
        str(directory / "%05d.ts"),
    ]  # fmt: skip


def cut(video: Path, directory: Path, segment_s: int) -> list[Path]:
    """Cut into .ts pieces of about segment_s seconds. Streams are copied when the video has a keyframe often enough
    (no quality lost, and fast); otherwise it is re-encoded so every cut falls on a keyframe."""
    try:
        expected = max(1, math.ceil(ff.probe(video).duration_s / segment_s - 0.05))
    except ff.RenderError as exc:
        raise DownloadError(f"the downloaded file cannot be read: {exc}") from exc
    for copy in (True, False):
        for old in directory.glob("*.ts"):
            old.unlink()
        try:
            run(_command(video, directory, segment_s, copy=copy), "ffmpeg", CUT_TIMEOUT_S)
        except DownloadError:
            if not copy:
                raise
            continue
        pieces = sorted(directory.glob("*.ts"))
        if pieces and (not copy or len(pieces) >= expected):
            return pieces
    raise DownloadError("ffmpeg produced no segments")


def ingest(
    engine: sa.Engine,
    store: MediaStore,
    video: Path,
    *,
    stream_id: uuid.UUID,
    video_id: str,
    profile: cs.Profile,
    source_id: uuid.UUID | None,
    segment_s: int,
    workdir: Path | None = None,
    before: Callable[[sa.Connection], None] | None = None,
) -> list[uuid.UUID]:
    """Store the segments of `video` for the stream `stream_id` and make an understand job for each new one. Safe to
    run twice: a segment already stored gets no second job. `before` runs in the same transaction first (the replay
    tool uses it to create the stream row). Returns the new jobs' ids; nothing is stored on an error."""
    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="ingest-"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        timeline: list[tuple[Path, float, float]] = []
        at = 0.0
        for piece in cut(video, work, segment_s):
            length = ff.probe(piece).duration_s
            timeline.append((piece, at, at + length))
            at += length
        keys = [f"segments/{stream_id}/{i:05d}.ts" for i in range(len(timeline))]
        for key, (piece, _, _) in zip(keys, timeline, strict=True):
            store.put_file(key, piece)
        new: list[uuid.UUID] = []
        with engine.begin() as conn:
            if before is not None:
                before(conn)
            for index, (key, (_, start, end)) in enumerate(zip(keys, timeline, strict=True)):
                fresh = conn.execute(
                    sa.text(
                        "INSERT INTO segments (stream_id, idx, key, start_s, end_s) VALUES (:s, :i, :k, :a, :b) "
                        "ON CONFLICT (stream_id, idx) DO NOTHING RETURNING id"
                    ),
                    {"s": stream_id, "i": index, "k": key, "a": start, "b": end},
                ).first()
                if fresh is None:
                    continue
                new.append(
                    jobs.create_job(
                        conn,
                        profile,
                        stage="understand",
                        source_id=source_id,
                        media_key=key,
                        data={
                            "live": True,
                            "stream_id": str(stream_id),
                            "video_id": video_id,
                            "segment": index,
                            "start_s": start,
                            "end_s": end,
                        },
                    )  # fmt: skip
                )
        return new
    finally:
        shutil.rmtree(work, ignore_errors=True)
