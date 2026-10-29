"""TikTok, assisted: the approved clip and its caption are sent to your Telegram and you post it from the TikTok app.

TikTok's Content Posting API posts publicly only for audited apps, and ours is a personal tool, so until that changes
this is the way (accounts.yaml: `tiktok.posting: assisted`). The clip goes as a file (a document, so Telegram does
not recompress it) and the caption as a separate message to copy as it is. A clip over the bot upload limit goes as
a lighter copy; the stored clip is never touched. The post counts as done once Telegram has the message, which is
all this service can know: posting it is up to you.

With Cloudflare R2 set up (accounts.yaml `media_host`, keys in .env) the clip is stored there as it is instead, and you
get a download link with a Delete file button: no size limit and no lighter copy. A file goes when you press the button
or after `max_age_h`; the review service only records the press (it holds no storage keys) and this service, which
does, deletes on its next sweep. Above `max_total_gb` stored, clips are sent as files again."""

from __future__ import annotations

import logging
import shutil
import subprocess  # nosec B404
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from common import jobs
from common.media import LocalStore
from common.r2 import R2
from common.rooms import Rooms
from common.telegram import Telegram, TelegramError
from services.publisher import accounts as acc
from services.publisher import hosted, posts
from services.publisher.base import PlatformPublisher, SendError, Sent
from services.publisher.clipstore import ClipStore
from services.render import ffmpeg as ff

log = logging.getLogger(__name__)
MAX_BYTES = 47 * 1024 * 1024  # bots may upload about 50 MB; this leaves room
AUDIO_KBPS = 128
MIN_VIDEO_KBPS = 300  # below this a clip looks too poor to be worth posting
SAFETY = 0.9  # a one-pass encode lands near its target, not exactly on it
RECODE_TIMEOUT_S = 900


def fit(file: Path, folder: Path, max_bytes: int) -> Path:
    """`file` itself when it fits, else a copy in `folder` with a bitrate chosen so it does."""
    if file.stat().st_size <= max_bytes:
        return file
    try:
        duration = ff.probe(file).duration_s
    except ff.RenderError as exc:
        raise SendError(f"the clip cannot be read: {exc}") from None
    video_kbps = int(max_bytes * 8 / 1000 / max(duration, 1) * SAFETY) - AUDIO_KBPS
    if video_kbps < MIN_VIDEO_KBPS:
        raise SendError(f"the clip is {duration:.0f} s long: too long to send through a bot at a usable quality")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SendError("ffmpeg is not installed")
    out = folder / "for-telegram.mp4"
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-protocol_whitelist", "file",
        "-i", str(file), "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{video_kbps}k",
        "-maxrate", f"{video_kbps}k", "-bufsize", f"{2 * video_kbps}k", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-movflags", "+faststart", "-y", str(out),
    ]  # fmt: skip
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=RECODE_TIMEOUT_S, check=False)  # noqa: S603  # nosec B603
    except subprocess.TimeoutExpired:
        raise SendError("making a lighter copy of the clip timed out", retryable=True) from None
    if done.returncode != 0 or not out.is_file():
        raise SendError(f"making a lighter copy failed: {done.stderr.strip()[-200:]}")
    if out.stat().st_size > max_bytes:
        raise SendError("the lighter copy is still too big for a bot")
    return out


class TikTokAssistedPublisher(PlatformPublisher):
    platform = "tiktok"
    text_key = "tiktok"

    def __init__(
        self,
        engine: sa.Engine,
        store: LocalStore,
        accounts: acc.Accounts,
        bot: Telegram,
        chats: list[int],
        *,
        max_bytes: int = MAX_BYTES,
        r2: R2 | None = None,
        host: acc.MediaHost | None = None,
        rooms: Rooms | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(engine, store, accounts, **kw)
        self.bot, self.chats, self.max_bytes = bot, chats, max_bytes
        self.rooms = rooms or Rooms(None, {})
        if (r2 is None) != (host is None):
            raise ValueError("R2 needs both the store and the media_host settings, or neither")
        self.r2, self.host = r2, host
        self.clips = ClipStore(engine, r2, host) if r2 is not None and host is not None else None

    def send(
        self,
        job: jobs.Job,
        rules: acc.AccountRules,
        post: posts.Post,
        file: Path,
        text: dict[str, Any],
        save_session: Callable[[str], None],
    ) -> Sent:
        if (post["session_url"] or "").startswith("tg:"):  # delivered already (a restart before the post was marked)
            return Sent("telegram:" + post["session_url"][3:], None)
        title = str(text.get("title", ""))[:120]
        if self.r2 is not None and self.host is not None and self._room_for(self.host, file):
            ids = self._send_link(self.r2, self.host, job, rules, post, file, text, title)
        else:
            ids = self._send_file(rules, file, text, title)
        save_session("tg:" + ids)
        return Sent("telegram:" + ids, None)

    def _room_for(self, host: acc.MediaHost, file: Path) -> bool:
        """Whether R2 still has room under the size cap (below the free tier). If not, the clip goes as a file."""
        with self.engine.connect() as conn:
            stored = hosted.total_bytes(conn)
        return stored + file.stat().st_size <= host.max_total_gb * 1e9

    def _send_file(self, rules: acc.AccountRules, file: Path, text: dict[str, Any], title: str) -> str:
        label = f"TikTok for {rules.id}: {title}"
        sent: list[int] = []
        with tempfile.TemporaryDirectory(prefix="tiktok-") as tmp:
            to_send = fit(file, Path(tmp), self.max_bytes)
            try:
                for where in self.rooms.where("approval", self.chats):
                    sent.append(self.bot.send_document(where.chat_id, to_send, label, thread_id=where.thread_id))
                    self.bot.send_message(where.chat_id, str(text["text"]), thread_id=where.thread_id)
            except TelegramError as exc:
                raise SendError(str(exc), retryable=True) from None
        return ",".join(str(i) for i in sent)

    def _send_link(
        self,
        r2: R2,
        host: acc.MediaHost,
        job: jobs.Job,
        rules: acc.AccountRules,
        post: posts.Post,
        file: Path,
        text: dict[str, Any],
        title: str,
    ) -> str:
        key = ClipStore.key(job["id"])  # the copy stored when the clip went for approval, or stored now if it is gone
        try:
            link = ClipStore(self.engine, r2, host).store(job["id"], file, keep_h=host.keep_after_approval_h)
        except Exception as exc:  # the store's own errors carry no secret; any failure here is worth another try
            raise SendError(f"could not store the clip in R2: {type(exc).__name__}", retryable=True) from None
        size = file.stat().st_size
        with self.engine.begin() as conn:
            file_id = hosted.add(conn, post["id"], key, size, [])  # first, so a Delete press always finds it
        message = (
            f"TikTok for {rules.id}: {title}\n\nDownload ({size / 1e6:.1f} MB):\n{link}\n\nThe file stays for "
            f"{host.keep_after_approval_h:g} h after you approved the clip, or press the button once you have "
            "posted it."
        )
        chats: list[dict[str, int]] = []
        try:
            for where in self.rooms.where("approval", self.chats):
                message_id = self.bot.send_message(
                    where.chat_id, message, buttons=[[("🗑 Delete file", f"x:{file_id}")]], thread_id=where.thread_id
                )
                chats.append({"chat_id": where.chat_id, "message_id": message_id})
                self.bot.send_message(where.chat_id, str(text["text"]), thread_id=where.thread_id)
        except TelegramError as exc:
            raise SendError(str(exc), retryable=True) from None
        with self.engine.begin() as conn:
            hosted.add(conn, post["id"], key, size, chats)
        return ",".join(str(c["message_id"]) for c in chats)

    def housekeeping(self) -> None:
        """Delete from R2 what you asked to delete and what is too old, and say so in the message. A delete that fails
        is tried again on the next sweep; a message that can no longer be edited does not stop it."""
        if self.r2 is None or self.host is None:
            return
        with self.engine.connect() as conn:
            due = hosted.due(conn, max_age_h=self.host.max_age_h)
        for file, why in due:
            try:
                self.r2.delete(file.key)
            except Exception:
                log.exception("could not delete %s from R2; trying again next sweep", file.key)
                continue
            with self.engine.begin() as conn:
                hosted.mark_deleted(conn, file.id)
            note = (
                "🗑 File deleted from storage." if why == "asked" else "⏰ Expired: the file was deleted from storage."
            )
            for chat in file.chats:
                try:
                    self.bot.edit_message_text(chat["chat_id"], chat["message_id"], note)
                except TelegramError as exc:
                    log.info("could not update the message of %s: %s", file.key, exc)
