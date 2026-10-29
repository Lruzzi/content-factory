"""Post an approved clip as a Facebook Page Reel (platform `facebook`).

The three steps are start, upload and finish. The Reel's video id is saved in the post row as soon as it exists, and
`|finished` is added once the finish call went through. A repeat then resumes where it stopped: an upload carries on
from the byte Facebook reports, a finished Reel is never finished again, and one Facebook already published is just
recognised. Facebook publishes a few seconds after the finish call: until then the post stays waiting."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from common import jobs
from services.publisher import accounts as acc
from services.publisher import posts
from services.publisher.base import Pending, SendError, Sent
from services.publisher.meta import Meta, MetaError
from services.publisher.meta_publisher import MetaPublisher

# Meta's docs say 3-90 s, but a 2 min and a 5 min Reel were both published through the API on 2026-10-24, so the upper
# bound is the longest length actually tested. Raise it only after testing a longer one.
MIN_S, MAX_S = 3, 300
BROKEN = ("error", "expired", "upload_failed")


class FacebookPublisher(MetaPublisher):
    platform = "facebook"
    text_key = "facebook"

    @staticmethod
    def _read(read: Callable[[], Any], finished: bool) -> Any:
        """A read of the Reel. Right after it was finished Facebook can answer code 100 ("object does not exist") for a
        moment although it is there and published (seen on 2026-10-28); that is "not yet", so the clip is looked at
        again on the next sweep. Before it was finished the same answer is a real error."""
        try:
            return read()
        except MetaError as exc:
            if finished and exc.code == 100:
                return None
            raise

    def _sent(self, meta: Meta, video_id: str) -> Sent:
        link = self._read(lambda: meta.fb_permalink(video_id), True)
        if link is None:
            raise Pending
        return Sent(video_id, link)

    def send(
        self,
        job: jobs.Job,
        rules: acc.AccountRules,
        post: posts.Post,
        file: Path,
        text: dict[str, Any],
        save_session: Callable[[str], None],
    ) -> Sent:
        page = self._cred(rules, "page_id_env")
        self._cred(rules, "token_env")
        length = ((job["data"] or {}).get("render", {}).get("output", {})).get("duration_s")
        if length is not None and not MIN_S <= float(length) <= MAX_S:
            raise SendError(
                f"a Facebook Reel must be {MIN_S} to {MAX_S} seconds long; this clip is {float(length):.0f}"
            )
        meta = self.meta_for(rules)
        video_id, _, marker = (post["session_url"] or "").partition("|")
        finished = marker == "finished"  # from here on a Reel Facebook "cannot find yet" is only not ready, not wrong
        try:
            if not video_id:
                video_id = meta.fb_reel_start(page)
                save_session(video_id)
            status = self._read(lambda: meta.fb_status(video_id), finished)
            if status is None:
                raise Pending
            if status.published:
                return self._sent(meta, video_id)
            if status.video_status in BROKEN:
                raise SendError(f"Facebook could not process the Reel ({status.video_status})")
            if not finished:
                if not status.upload_complete:
                    meta.fb_upload(video_id, file, offset=status.bytes_transferred)
                if not self._wait_until(lambda: meta.fb_status(video_id).upload_complete):
                    raise Pending
                meta.fb_reel_finish(page, video_id, description=text["text"], title=text["title"][:100])
                save_session(f"{video_id}|finished")
            if not self._wait_until(lambda: bool(self._read(lambda: meta.fb_status(video_id).published, True))):
                raise Pending
            return self._sent(meta, video_id)
        except MetaError as exc:
            raise self._translate(exc, rules.id) from None
