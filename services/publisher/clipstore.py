"""The one stored copy of a clip in R2, shared by the review message, TikTok and Instagram.

It is uploaded when the clip is sent for approval, so the approval message can carry a link to the full quality file;
rejecting the clip (or letting its review expire) deletes it, approving it keeps it a fixed time (`media_host.
keep_after_approval_h`), and TikTok and Instagram use the same copy instead of storing their own. If it is missing
(uploads were off, it was swept, the posting came days later) the publisher stores it again from the local clip.

Whoever holds the R2 keys runs this: the review service and the two publishers. Deleting is best effort and idempotent;
a file that could not be deleted stays recorded and the next sweep tries again."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import sqlalchemy as sa

from common.r2 import R2
from services.publisher import accounts as acc
from services.publisher import hosted

log = logging.getLogger(__name__)


class ClipStore:
    def __init__(self, engine: sa.Engine, r2: R2, host: acc.MediaHost) -> None:
        self.engine, self.r2, self.host = engine, r2, host

    @staticmethod
    def key(job_id: uuid.UUID | str) -> str:
        return f"review/{job_id}.mp4"

    def store(self, job_id: uuid.UUID | str, file: Path, *, keep_h: float) -> str:
        """Make sure the clip is stored (uploading it unless it already is) and return a link that downloads it. Errors
        are raised: the publishers turn them into a retry."""
        key = self.key(job_id)
        if not self.r2.exists(key):
            size = self.r2.upload(key, file)
            with self.engine.begin() as conn:
                hosted.add(conn, None, key, size, [], keep_h=keep_h)
        return self.r2.presign(key, ttl_s=self.host.link_ttl_min * 60, filename=f"{job_id}.mp4")

    def put(self, job_id: uuid.UUID | str, file: Path, *, pending_h: float) -> str | None:
        """Store the clip for review. It stays at most `pending_h` hours (an hour more than the review lasts) unless it
        is approved. None, with a log line, when it cannot be stored: the approval then goes out without the link."""
        try:
            with self.engine.connect() as conn:
                stored = hosted.total_bytes(conn)
            if stored + file.stat().st_size > self.host.max_total_gb * 1e9:
                log.warning(
                    "R2 holds %.1f GB already; the clip %s goes for review without a link", stored / 1e9, job_id
                )
                return None
            return self.store(job_id, file, keep_h=pending_h + 1)
        except Exception as exc:  # the store's own errors carry no secret
            log.warning(
                "could not store the clip %s in R2 (%s); it goes for review without a link", job_id, type(exc).__name__
            )
            return None

    def link(self, job_id: uuid.UUID | str) -> str | None:
        """A link to the stored clip, or None when it is not stored."""
        key = self.key(job_id)
        try:
            with self.engine.connect() as conn:
                if hosted.live(conn, key) is None:
                    return None
            return self.r2.presign(key, ttl_s=self.host.link_ttl_min * 60, filename=f"{job_id}.mp4")
        except Exception as exc:
            log.warning("could not make a link for %s (%s)", key, type(exc).__name__)
            return None

    def approved(self, job_id: uuid.UUID | str) -> None:
        """The clip was approved: keep it for the set time from now."""
        with self.engine.begin() as conn:
            hosted.set_keep(conn, self.key(job_id), self.host.keep_after_approval_h)

    def discard(self, job_id: uuid.UUID | str) -> None:
        """The clip was rejected or its review expired: delete it now."""
        key = self.key(job_id)
        try:
            self.r2.delete(key)
        except Exception as exc:
            log.warning("could not delete %s from R2 (%s); the sweep will try again", key, type(exc).__name__)
            with self.engine.begin() as conn:
                hosted.set_keep(conn, key, 0)  # due now, so the next sweep retries
            return
        with self.engine.begin() as conn:
            hosted.forget(conn, key)

    def sweep(self) -> int:
        """Delete what is due (rejected, expired, past its keep time, asked for). Returns how many were deleted."""
        with self.engine.connect() as conn:
            due = hosted.due(conn, max_age_h=self.host.max_age_h)
        gone = 0
        for file, _why in due:
            try:
                self.r2.delete(file.key)
            except Exception as exc:
                log.warning("could not delete %s from R2 (%s); trying again next sweep", file.key, type(exc).__name__)
                continue
            with self.engine.begin() as conn:
                hosted.mark_deleted(conn, file.id)
            gone += 1
        return gone
