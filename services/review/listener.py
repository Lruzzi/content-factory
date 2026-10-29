"""Act on Telegram button taps and exact commands. Only the allowed Telegram user ids count; everything else is ignored
without an answer, so a stranger who finds the bot learns nothing. Of what you type, only a message that is exactly one
command word ("/status") in a private chat with you is read (commands.parse); free text is never read."""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections.abc import Callable
from typing import Any

import sqlalchemy as sa

from common import jobs
from common.rooms import Rooms
from common.telegram import Telegram, TelegramError
from services.publisher import hosted
from services.publisher.clipstore import ClipStore
from services.review import commands as cmd
from services.review import controls, decide, reasons

log = logging.getLogger(__name__)
SWITCH = re.compile(r"(ps|pz|rs):(publish|live|ended|intake|src:[A-Za-z0-9_.-]{1,40})")
BUDGET = re.compile(r"bz:(\d{1,5}(?:\.\d{1,2})?)")  # confirm a new monthly cap, in dollars
RULE = re.compile(r"rl:(cap|spacing|clips|window):(\d{1,4}|any|\d{4}-\d{4}(?:,\d{4}-\d{4}){0,3})")  # confirm a rule
REASON = re.compile(r"w:([a-z]{2,10}):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
CALLBACK = re.compile(r"([arxq]):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
WORDS = {"approved": "✅ Approved", "rejected": "❌ Rejected", "expired": "⏰ Expired"}


def _show(
    bot: Telegram, chats: list[dict[str, Any]], state: str, title: str, *, note: str = "",
    buttons: list[list[tuple[str, str]]] | None = None,
) -> None:  # fmt: skip
    for chat in chats:
        try:
            bot.edit_caption(chat["chat_id"], chat["message_id"], f"{WORDS[state]}: {title}{note}", buttons)
        except TelegramError as exc:
            log.warning("could not update a review message: %s", exc)


def _explain(bot: Telegram, tap: dict[str, Any], review: dict[str, Any]) -> None:
    """A tap that changed nothing: say why in a popup, and take the buttons off the message that was tapped, so a
    button for a clip that is gone (or already settled) never sits there doing nothing."""
    done = review.get("state")
    if done in WORDS:
        text, caption = f"Already decided or expired ({WORDS[done]})", f"{WORDS[done]}: {review.get('title', '')}"
    else:
        text = "This clip no longer exists"
        caption = "⚠️ This clip no longer exists (it was removed before you decided)"
    bot.answer_callback(tap["id"], text, alert=True)
    message = tap.get("message") or {}
    chat_id, message_id = (message.get("chat") or {}).get("id"), message.get("message_id")
    if isinstance(chat_id, int) and isinstance(message_id, int):
        bot.edit_caption(chat_id, message_id, caption)


def _reason(engine: sa.Engine, bot: Telegram, tap: dict[str, Any], code: str, job_id: uuid.UUID) -> None:
    """The reason buttons under a rejected clip. Skip takes them away; a code that is not on the list is ignored."""
    if code != reasons.SKIP and code not in reasons.REASONS:
        return
    try:
        if code != reasons.SKIP and not decide.record_reason(engine, job_id, code):
            bot.answer_callback(tap["id"], "Already answered, or that clip was not rejected")
            return
        bot.answer_callback(tap["id"], "Noted" if code != reasons.SKIP else "Skipped")
        review = decide.review_of(engine, job_id)
        note = f" ({reasons.REASONS[review['reason']]})" if review.get("reason") in reasons.REASONS else ""
        _show(bot, review.get("chats", []), "rejected", review.get("title", ""), note=note)
    except TelegramError as exc:
        log.warning("could not answer a tap: %s", exc)


def _delete_file(engine: sa.Engine, bot: Telegram, tap: dict[str, Any], file_id: uuid.UUID) -> None:
    with engine.begin() as conn:
        asked = hosted.request_delete(conn, file_id)
    try:
        bot.answer_callback(tap["id"], "Deleting the file…" if asked else "Already deleted or being deleted")
        message = tap.get("message") or {}
        if message.get("message_id") is not None and (message.get("chat") or {}).get("id") is not None:
            note = "🗑 Deleting the file…" if asked else "🗑 The file is already deleted."
            bot.edit_message_text(message["chat"]["id"], message["message_id"], note)  # no button left to press again
    except TelegramError as exc:
        log.warning("could not answer a tap: %s", exc)


def _handle_message(
    message: dict[str, Any], *, bot: Telegram, allowed: set[int], commands: cmd.Commands | None, rooms: Rooms
) -> None:
    sender = (message.get("from") or {}).get("id")
    chat = message.get("chat") or {}
    thread = message.get("message_thread_id")
    if commands is None or sender not in allowed:
        return  # a stranger: no answer, nothing read
    if chat.get("type") == "private" and chat.get("id") == sender:
        where = (sender, None)  # your private chat with the bot
    elif rooms.is_commands_room(chat.get("id"), thread):
        where = (chat["id"], thread)  # the commands topic of the group, and only that topic
    else:
        return
    amount = cmd.budget_amount(message.get("text"))
    if amount is not None:  # the one thing read besides a bare command word: `/budget 25`, parsed strictly
        try:
            _ask_budget(bot, where, amount, commands)
        except TelegramError as exc:
            log.warning("could not answer a command: %s", exc)
        return
    change = cmd.rule_change(message.get("text"))
    if change is not None:  # `/cap 6`, `/spacing 120`, `/clips 6`, `/window 10:00-23:00`: parsed strictly, then asked
        try:
            _ask_rule(bot, where, change, commands)
        except TelegramError as exc:
            log.warning("could not answer a command: %s", exc)
        return
    name = cmd.parse(message.get("text"))
    if name is None:
        return  # free text is never read
    chat_id, thread_id = where
    try:
        if name == "status":
            bot.send_message(chat_id, commands.status_html(), thread_id=thread_id, html=True)
        elif name == "posts":
            bot.send_message(chat_id, commands.posts(), thread_id=thread_id)
        elif name == "health":
            bot.send_message(chat_id, commands.health(), thread_id=thread_id)
        elif name == "budget":
            bot.send_message(chat_id, commands.budget_text(), thread_id=thread_id)
        elif (
            name == "rules" or name in cmd.RULE_NAMES
        ):  # a bare /cap and the like show the rules and how to change them
            bot.send_message(chat_id, commands.rules_text(), thread_id=thread_id)
        elif name in ("pause", "resume"):
            text, buttons = commands.pause_menu() if name == "pause" else commands.resume_menu()
            bot.send_message(chat_id, text, buttons=buttons or None, thread_id=thread_id)
        elif name == "dead":
            _send_dead(bot, chat_id, thread_id, commands)
        else:
            bot.send_message(chat_id, commands.help(), thread_id=thread_id)
    except TelegramError as exc:
        log.warning("could not answer a command: %s", exc)


def _send_dead(bot: Telegram, chat_id: int, thread_id: int | None, commands: cmd.Commands) -> None:
    listed, total = commands.dead()
    if not listed:
        bot.send_message(chat_id, "No dead jobs.", thread_id=thread_id)
        return
    bot.send_message(chat_id, f"{total} dead job(s); the newest {len(listed)}:", thread_id=thread_id)
    for job in listed:
        kind = f" / {job.kind}" if job.kind else ""
        bot.send_message(
            chat_id,
            f"{str(job.id)[:8]}: {job.stage}{kind}\n{job.error}",
            buttons=[[("↻ Requeue", f"q:{job.id}")]],
            thread_id=thread_id,
        )


def _switch(engine: sa.Engine, bot: Telegram, tap: dict[str, Any], data: str, sender: int) -> None:
    """A pause or resume button: `ps` asks, `pz` does it, `rs` resumes, `pc` cancels."""
    message = tap.get("message") or {}
    chat_id, message_id = (message.get("chat") or {}).get("id"), message.get("message_id")
    target = (chat_id, message_id) if isinstance(chat_id, int) and isinstance(message_id, int) else None
    try:
        if data == "pc":
            bot.answer_callback(tap["id"], "Cancelled")
            if target:
                bot.edit_message_text(*target, "Cancelled: nothing was changed.")
            return
        action, what = data.split(":", 1)
        if action == "ps":
            bot.answer_callback(tap["id"], "Confirm?")
            if target:
                bot.edit_message_text(
                    *target, controls.question(what), [[("Yes, pause", f"pz:{what}"), ("Cancel", "pc")]]
                )
            return
        done, text = (controls.pause if action == "pz" else controls.resume)(engine, what, sender)
        bot.answer_callback(tap["id"], text, alert=not done and "claim" in text)
        if target and (action == "pz" or done):  # a refused resume keeps its message (a claim is explained there)
            bot.edit_message_text(*target, text)
    except TelegramError as exc:
        log.warning("could not answer a tap: %s", exc)


def _requeue(
    engine: sa.Engine,
    bot: Telegram,
    tap: dict[str, Any],
    job_id: uuid.UUID,
    requeue_job: Callable[[uuid.UUID, str], object] | None,
) -> None:
    with engine.begin() as conn:
        stage = conn.execute(
            sa.text("SELECT stage FROM jobs WHERE id = :i AND status = 'dead'"), {"i": job_id}
        ).scalar_one_or_none()
        done = stage is not None and jobs.requeue_dead(conn, job_id)
    if done and stage is not None and requeue_job is not None:
        try:
            requeue_job(job_id, stage)
        except Exception:
            log.exception("could not queue job %s; it stays queued in Postgres for the reconciler", job_id)
    try:
        bot.answer_callback(tap["id"], "Requeued" if done else "That job is not dead (any more)")
        message = tap.get("message") or {}
        chat_id, message_id = (message.get("chat") or {}).get("id"), message.get("message_id")
        if done and isinstance(chat_id, int) and isinstance(message_id, int):
            bot.edit_message_text(chat_id, message_id, f"↻ Requeued: job {str(job_id)[:8]} goes through again")
    except TelegramError as exc:
        log.warning("could not answer a tap: %s", exc)


def _budget(
    engine: sa.Engine, bot: Telegram, tap: dict[str, Any], data: str, sender: int, commands: cmd.Commands | None
) -> None:
    """The Yes button of `/budget <amount>`: set the cap (the typo guard in workers.yaml is enforced)."""
    limits = commands.spend_limits if commands else None
    if commands is None or limits is None:
        return
    message = tap.get("message") or {}
    chat_id, message_id = (message.get("chat") or {}).get("id"), message.get("message_id")
    done, text = controls.set_budget(engine, limits, float(data.split(":", 1)[1]), sender)
    try:
        bot.answer_callback(tap["id"], text)
        if done and isinstance(chat_id, int) and isinstance(message_id, int):
            bot.edit_message_text(chat_id, message_id, commands.budget_text())
    except TelegramError as exc:
        log.warning("could not answer a tap: %s", exc)


def _ask_rule(bot: Telegram, where: tuple[int, int | None], change: tuple[str, str], commands: cmd.Commands) -> None:
    """`/cap 6` and the like: say what would change and wait for the Yes tap; a refused value is not asked."""
    chat_id, thread_id = where
    name, raw = change
    ok, text, token = controls.rule_question(commands.engine, commands.rule_ceilings, name, raw)
    bot.send_message(
        chat_id, text, buttons=[[("Yes, change", f"rl:{name}:{token}"), ("Cancel", "pc")]] if ok else None,
        thread_id=thread_id,
    )  # fmt: skip


def _rule(
    engine: sa.Engine,
    bot: Telegram,
    tap: dict[str, Any],
    match: re.Match[str],
    sender: int,
    commands: cmd.Commands | None,
) -> None:
    """The Yes button of a rule change."""
    if commands is None:
        return
    message = tap.get("message") or {}
    chat_id, message_id = (message.get("chat") or {}).get("id"), message.get("message_id")
    done, text = controls.set_rule(engine, commands.rule_ceilings, match.group(1), match.group(2), sender)
    try:
        bot.answer_callback(tap["id"], text)
        if done and isinstance(chat_id, int) and isinstance(message_id, int):
            bot.edit_message_text(chat_id, message_id, commands.rules_text())
    except TelegramError as exc:
        log.warning("could not answer a tap: %s", exc)


def _ask_budget(bot: Telegram, where: tuple[int, int | None], amount: float, commands: cmd.Commands) -> None:
    """`/budget 25`: say what would change and wait for the Yes tap; a cap the guard refuses is not even asked."""
    chat_id, thread_id = where
    if commands.spend_limits is None:
        bot.send_message(chat_id, commands.budget_text(), thread_id=thread_id)
        return
    ok, text = controls.budget_question(commands.engine, commands.spend_limits, amount)
    bot.send_message(
        chat_id, text, buttons=[[("Yes, change", f"bz:{amount:.2f}"), ("Cancel", "pc")]] if ok else None,
        thread_id=thread_id,
    )  # fmt: skip


def handle_update(
    update: dict[str, Any],
    *,
    engine: sa.Engine,
    bot: Telegram,
    allowed: set[int],
    publish_job: Callable[[uuid.UUID], object] | None,
    commands: cmd.Commands | None = None,
    requeue_job: Callable[[uuid.UUID, str], object] | None = None,
    rooms: Rooms | None = None,
    clips: ClipStore | None = None,
) -> None:
    message = update.get("message")
    if isinstance(message, dict):
        _handle_message(message, bot=bot, allowed=allowed, commands=commands, rooms=rooms or Rooms(None, {}))
        return
    tap = update.get("callback_query")
    if not isinstance(tap, dict):
        return
    sender = (tap.get("from") or {}).get("id")
    if sender not in allowed:
        log.warning("ignored a button tap from a user who is not allowed")
        return
    data = tap.get("data") or ""
    if data == "pc" or SWITCH.fullmatch(data):
        _switch(engine, bot, tap, data, sender)
        return
    if BUDGET.fullmatch(data):
        _budget(engine, bot, tap, data, sender, commands)
        return
    if (rule := RULE.fullmatch(data)) is not None:
        _rule(engine, bot, tap, rule, sender, commands)
        return
    why = REASON.fullmatch(data)
    if why is not None:
        _reason(engine, bot, tap, why.group(1), uuid.UUID(why.group(2)))
        return
    match = CALLBACK.fullmatch(data)
    if match is None:
        return
    if match.group(1) == "q":  # Requeue a dead job
        _requeue(engine, bot, tap, uuid.UUID(match.group(2)), requeue_job)
        return
    if match.group(1) == "x":  # Delete file: only recorded here; the publisher that holds the storage keys deletes it
        _delete_file(engine, bot, tap, uuid.UUID(match.group(2)))
        return
    job_id, state = uuid.UUID(match.group(2)), "approved" if match.group(1) == "a" else "rejected"
    try:
        if not decide.decide(engine, job_id, state, by=sender):
            _explain(bot, tap, decide.review_of(engine, job_id))
            return
        if state == "approved" and publish_job is not None:
            try:
                publish_job(job_id)
            except Exception:
                log.exception("could not queue job %s; it stays queued in Postgres for the reconciler", job_id)
        bot.answer_callback(tap["id"], WORDS[state])
        review = decide.review_of(engine, job_id)
        link = None
        if clips is not None:  # the full quality copy in R2: kept for a day once approved, gone at once if rejected
            if state == "approved":
                clips.approved(job_id)
                link = clips.link(job_id)
            else:
                clips.discard(job_id)
        if state == "rejected":  # decided already; the reason is optional and only helps the numbers
            _show(
                bot, review.get("chats", []), state, review.get("title", ""),
                note="\nWhy? (optional)", buttons=reasons.buttons(job_id),
            )  # fmt: skip
        else:
            _show(
                bot, review.get("chats", []), state, review.get("title", ""),
                buttons=[[("⬇ Full quality", link)]] if link else None,
            )  # fmt: skip
    except TelegramError as exc:
        log.warning("could not answer a tap: %s", exc)


def finish_expired(engine: sa.Engine, bot: Telegram, clips: ClipStore | None) -> None:
    """Close the reviews nobody answered in time, delete their stored copies, and sweep every copy that is due."""
    for gone in decide.expire_due(engine):
        if clips is not None:
            clips.discard(gone.job_id)
        _show(bot, gone.chats, "expired", gone.title)
    if clips is not None:
        clips.sweep()


def poll_forever(
    *,
    engine: sa.Engine,
    bot: Telegram,
    allowed: set[int],
    publish_job: Callable[[uuid.UUID], object] | None,
    stop: threading.Event,
    wait_s: int = 25,
    commands: cmd.Commands | None = None,
    requeue_job: Callable[[uuid.UUID, str], object] | None = None,
    rooms: Rooms | None = None,
    clips: ClipStore | None = None,
) -> None:
    """Long-poll Telegram for taps, and finish the clips whose review time ran out."""
    offset: int | None = None
    while not stop.is_set():
        try:
            for update in bot.get_updates(offset=offset, timeout_s=wait_s):
                offset = update["update_id"] + 1
                handle_update(
                    update, engine=engine, bot=bot, allowed=allowed, publish_job=publish_job, commands=commands,
                    requeue_job=requeue_job, rooms=rooms, clips=clips,
                )  # fmt: skip
            finish_expired(engine, bot, clips)
        except TelegramError as exc:
            log.warning("telegram: %s", exc)
            stop.wait(5)
        except Exception:
            log.exception("review loop failed; trying again")
            stop.wait(5)
