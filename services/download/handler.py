"""The download stage as a worker handler: fetch the stretch of a video the job asks for, cut it into segments and hand
each to understand, exactly as a live recording would. Everything about HOW comes from the job's `spec` snapshot (the
height, the segment length), never from the YAML. An engine service: it never reads targets, accounts or tokens."""

from __future__ import annotations

import re
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import sqlalchemy as sa

import config_schema as cs
from common import jobs
from common.media import MediaStore
from services.download import ingest, ytdlp

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
Fetch = Callable[..., Path]  # (video_id, folder, *, start_s, length_s, height) -> the downloaded file


def check_video_id(video_id: str) -> str:
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError(f"not a video id: {video_id[:30]!r}")
    return video_id


def _needs(data: dict[str, Any]) -> tuple[str, uuid.UUID, int, int]:
    for field in ("video_id", "stream_id", "start_s", "length_s"):
        if data.get(field) is None:
            raise ValueError(f"the download job has no {field}")
    return (
        check_video_id(str(data["video_id"])),
        uuid.UUID(str(data["stream_id"])),
        int(data["start_s"]),
        int(data["length_s"]),
    )


def make_handler(
    engine: sa.Engine,
    store: MediaStore,
    *,
    fetch: Fetch = ytdlp.fetch,
    publish_job: Callable[[uuid.UUID], object] | None = None,
    scratch: Path | None = None,
) -> Callable[[jobs.Job], None]:
    def handle(job: jobs.Job) -> None:
        profile = cs.Profile.model_validate(job["spec"])
        video_id, stream_id, start_s, length_s = _needs(job["data"] or {})
        with tempfile.TemporaryDirectory(prefix="download-", dir=scratch) as tmp:
            folder = Path(tmp)
            video = fetch(
                video_id, folder, start_s=start_s, length_s=length_s, height=int(profile.download["max_height"])
            )
            ingest.ingest(
                engine, store, video, stream_id=stream_id, video_id=video_id, profile=profile,
                source_id=job.get("source_id"), segment_s=profile.live.segment_min * 60, workdir=folder / "cut",
            )  # fmt: skip
        if publish_job is not None:
            # every waiting understand job of this stream, new or left over from an earlier run whose queue message
            # was lost: offering a job twice is safe, the stage skips what is done
            with engine.connect() as conn:
                waiting = conn.execute(
                    sa.text(
                        "SELECT id FROM jobs WHERE stage = 'understand' AND status = 'queued' "
                        "AND data->>'stream_id' = :s"
                    ),
                    {"s": str(stream_id)},
                ).scalars()
                ids = list(waiting)
            for job_id in ids:
                publish_job(job_id)

    return handle
