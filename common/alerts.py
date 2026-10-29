"""Alerts: a line in the log, and a Telegram message to every allowed user. An alert is for something that needs a
person (a dead job, a failed post, an expired sign-in, a lost stream). It must never crash the service that raised it,
so a failed send is only logged. What goes into the message often came from a platform or a model, so control and
direction-override characters are removed, the text is cut short, and no formatting is switched on."""

from __future__ import annotations

import logging
import time
import unicodedata
from collections.abc import Callable, Mapping

from common.rooms import Rooms
from common.telegram import Telegram, TelegramError

log = logging.getLogger("alerts")
MAX_CHARS = 1000
REPEAT_S = 1800


def _clean(text: str) -> str:
    kept = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"))
    return kept.strip()[:MAX_CHARS]


def make_alerter(
    service: str,
    env: Mapping[str, str],
    *,
    bot: Telegram | None = None,
    clock: Callable[[], float] = time.monotonic,
    rooms: Rooms | None = None,
) -> Callable[[str], None]:
    """An `alert(message)` function for one service. With TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USERS set it also
    messages those users, or posts in the alerts topic when a group is set up (`rooms`); the same text is sent at most
    once every half hour."""
    token, raw_ids = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_ALLOWED_USERS", "")
    try:
        users = [int(x) for x in raw_ids.replace(" ", "").split(",") if x]
    except ValueError:
        raise ValueError("TELEGRAM_ALLOWED_USERS must be numeric Telegram user ids separated by commas") from None
    telegram = bot if bot is not None else (Telegram(token) if token and users else None)
    last: dict[str, float] = {}

    def alert(message: str) -> None:
        text = _clean(message)
        log.error("ALERT %s: %s", service, text)
        now = clock()
        if text in last and now - last[text] < REPEAT_S:
            return
        last[text] = now
        if telegram is None:
            return
        for where in (rooms or Rooms(None, {})).where("alerts", users):
            try:
                telegram.send_message(where.chat_id, f"⚠️ {service}: {text}", thread_id=where.thread_id)
            except TelegramError as exc:
                log.warning("could not send an alert: %s", exc)

    return alert


POST_MAX_CHARS = 3500


def make_poster(
    env: Mapping[str, str], *, room: str, rooms: Rooms | None = None, bot: Telegram | None = None
) -> Callable[[str], None]:
    """A `post(text)` function that puts a message in one room (for instance `monitoring`) as plain text, and never
    raises. Without a group it goes to each allowed user's private chat."""
    token, raw_ids = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_ALLOWED_USERS", "")
    users = [int(x) for x in raw_ids.replace(" ", "").split(",") if x.isdigit()]
    telegram = bot if bot is not None else (Telegram(token) if token and users else None)

    def post(message: str) -> None:
        text = _clean(message)[:POST_MAX_CHARS] if len(message) <= MAX_CHARS else _clean_long(message)
        log.info("POST %s: %s", room, text[:200])
        if telegram is None:
            return
        for where in (rooms or Rooms(None, {})).where(room, users):
            try:
                telegram.send_message(where.chat_id, text, thread_id=where.thread_id)
            except TelegramError as exc:
                log.warning("could not post in the %s room: %s", room, exc)

    return post


def _clean_long(text: str) -> str:
    kept = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"))
    return kept.strip()[:POST_MAX_CHARS]
