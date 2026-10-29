"""The review stage as a worker handler. With `review.required` on, send the clip to the allowed Telegram users and hold
the job until one of them taps (listener.py acts on the tap); with it off, pass the clip straight on. The text sent is
plain, and anything that came from the transcript has control characters stripped. A job delivered again while it
waits is not sent twice."""

from __future__ import annotations

import logging
import tempfile
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

import config_schema as cs
from common import jobs
from common.media import LocalStore
from common.rooms import Rooms
from common.telegram import Telegram, TelegramError
from services.publisher.clipstore import ClipStore
from services.review import decide, preview

log = logging.getLogger(__name__)


class ReviewError(RuntimeError):
    pass


def _clean(text: object, limit: int) -> str:
    kept = "".join(ch for ch in " ".join(str(text).split()) if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"))
    return kept[:limit]


def _caption(profile: cs.Profile, job: jobs.Job, title: str) -> str:
    clip = (job["data"] or {}).get("clip", {})
    hook = _clean((job["data"] or {}).get("render", {}).get("hook") or "", 120)
    lines = [
        title,
        f"Kind: {job.get('kind') or clip.get('kind', '?')}  |  score {clip.get('score', '?')}",
        f"Why: {_clean(clip.get('reason', ''), 250)}",
    ]
    if hook:
        lines.append(f"Hook: {hook}")
    if clip.get("llm") or clip.get("stt"):
        lines.append(f"LLM: {_clean(clip.get('llm', '?'), 80)}  |  STT: {_clean(clip.get('stt', '?'), 60)}")
    lines.append(f"Mode: {profile.mode}  |  posts to: {', '.join(profile.upload_accounts()) or 'nowhere'}")
    return "\n".join(lines)


def make_handler(
    engine: Engine,
    store: LocalStore,
    bot: Telegram,
    allowed: list[int],
    *,
    next_stage: str = "publish",
    rooms: Rooms | None = None,
    clips: ClipStore | None = None,
) -> Callable[[jobs.Job], str]:
    def handle(job: jobs.Job) -> str:
        profile = cs.Profile.model_validate(job["spec"])
        data: dict[str, Any] = job["data"] or {}
        if not profile.needs_approval():
            with engine.begin() as conn:
                jobs.set_result(conn, job["id"], data={"review": {"state": "auto"}})
            return next_stage
        if (data.get("review") or {}).get("state") == "pending":
            return jobs.WAIT  # the preview is already out
        source = store.path(job["media_key"])
        if not source.is_file():
            raise ReviewError(f"clip {job['media_key']!r} is not in the media store")
        title = _clean(((data.get("package") or {}).get("shorts") or {}).get("title", ""), 100)
        caption = _caption(profile, job, title)
        buttons = [[("✅ Approve", f"a:{job['id']}"), ("❌ Reject", f"r:{job['id']}")]]
        if clips is not None:  # the full quality file first, so the approval can carry a link to it
            pending_h = float(profile.review.get("expire_after_h", decide.DEFAULT_EXPIRE_H))
            link = clips.put(job["id"], source, pending_h=pending_h)
            if link:
                buttons.append([("⬇ Full quality", link)])
        chats: list[dict[str, int]] = []
        failure: TelegramError | None = None
        with tempfile.TemporaryDirectory() as tmp:
            small = preview.make_preview(source, Path(tmp) / "preview.mp4")
            for where in (rooms or Rooms(None, {})).where("approval", allowed):  # the approval topic, or each user
                try:
                    message_id = bot.send_video(where.chat_id, small, caption, buttons, thread_id=where.thread_id)
                    chats.append({"chat_id": where.chat_id, "message_id": message_id})
                except TelegramError as exc:
                    failure = exc
                    log.warning("could not send the review: %s", exc)
        if not chats:
            raise failure or ReviewError("no one to send the review to")
        with engine.begin() as conn:
            jobs.set_result(conn, job["id"], data={"review": {"state": "pending", "title": title, "chats": chats}})
        return jobs.WAIT

    return handle
