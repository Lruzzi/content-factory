"""Post an approved clip as an Instagram Reel (platform `reels`), with Instagram's resumable upload: no public address
for the video is needed.

A container is created and the video is uploaded into it; only then is the container's id saved in the post row. A crash
before that leaves an orphan container, which Instagram deletes after 24 hours and which posts nothing. With the id
saved, a repeat only checks the container: finished means publish it, published means find the Reel that was posted
(nothing is published twice), and still processing means wait."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from common import jobs
from services.publisher import accounts as acc
from services.publisher import posts
from services.publisher.base import Parked, Pending, SendError, Sent
from services.publisher.clipstore import ClipStore
from services.publisher.meta import Meta, MetaError
from services.publisher.meta_publisher import MetaPublisher

log = logging.getLogger(__name__)
STAMP = "%Y-%m-%dT%H:%M:%S%z"
FOUND_SLACK = timedelta(minutes=2)  # a Reel counts as ours if it appeared after the post was started, give or take


class InstagramPublisher(MetaPublisher):
    platform = "reels"
    text_key = "reels"

    def __init__(self, *args: Any, clips: ClipStore | None = None, **kw: Any) -> None:
        """With `clips` the clip goes to Instagram as a signed link to the stored copy, which Instagram fetches itself;
        else as a resumable upload."""
        super().__init__(*args, **kw)
        self.clips = clips

    def send(
        self,
        job: jobs.Job,
        rules: acc.AccountRules,
        post: posts.Post,
        file: Path,
        text: dict[str, Any],
        save_session: Callable[[str], None],
    ) -> Sent:
        ig_user = self._cred(rules, "ig_user_id_env")
        self._cred(rules, "token_env")
        meta = self.meta_for(rules)
        container = post["session_url"]
        try:
            if not container:
                if self.clips is not None:
                    container = self._container_from_link(meta, ig_user, job, file, text["text"])
                else:
                    container, uri = meta.ig_create_container(ig_user, caption=text["text"])
                    meta.ig_upload(uri, file)
                save_session(container)
            code = "IN_PROGRESS"

            def settled() -> bool:
                nonlocal code
                code = meta.ig_status(container)
                return code != "IN_PROGRESS"

            self._wait_until(settled)
            if code == "FINISHED":
                media = meta.ig_publish(ig_user, container)
                return Sent(media, meta.ig_permalink(media))
            if code == "PUBLISHED":
                return self._found(meta, ig_user, post)
            if code == "IN_PROGRESS":
                raise Pending
            raise SendError(f"Instagram reports the container as {code}")
        except MetaError as exc:
            raise self._translate(exc, rules.id) from None

    def _container_from_link(self, meta: Meta, ig_user: str, job: jobs.Job, file: Path, caption: str) -> str:
        """Give Instagram a signed link to the stored copy of the clip (stored now if it is not there) and let it fetch
        the clip itself. The copy is shared with the approval message and TikTok, and is deleted by its own rule."""
        if self.clips is None:
            raise SendError("no store for the clip", retryable=False)
        try:
            link = self.clips.store(job["id"], file, keep_h=self.clips.host.keep_after_approval_h)
        except Exception as exc:  # the store's own errors carry no secret; any failure here is worth another try
            raise SendError(f"could not store the clip in R2: {type(exc).__name__}", retryable=True) from None
        return meta.ig_create_container_from_url(ig_user, caption=caption, video_url=link)

    @staticmethod
    def _found(meta: Meta, ig_user: str, post: posts.Post) -> Sent:
        """The Reel that was published just before a crash: the newest one that appeared after this post was started."""
        started: datetime = post["created_at"]
        for media in meta.ig_recent_media(ig_user):
            if datetime.strptime(media["timestamp"], STAMP) >= started - FOUND_SLACK:
                return Sent(str(media["id"]), media.get("permalink"))
        raise Parked(
            "Instagram says the Reel was published but it could not be found: check the account before retrying"
        )
