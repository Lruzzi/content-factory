"""Find your Telegram group and its topics and write config/telegram.yaml: `python -m services.review.rooms_setup
["group name"] [--create]` (or `make telegram-rooms`). Run it while the review service is stopped (only one reader of
the bot's updates at a time), after you have made the group, turned Topics on, added the bot as an admin with Manage
topics, and posted one message in each topic (Telegram cannot list topics: a message is how a name is learned). What is
found is saved. A topic still missing is only created with `--create`, so it does not duplicate one you already made.
Each room that is known then gets a test message."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from common import rooms
from common.telegram import Telegram, TelegramError
from config_schema import ROOT
from services.review.commands import help_text

NAMES = {"approval": "Approval", "alerts": "Alerts", "monitoring": "Monitoring", "commands": "Commands"}


def main(argv: list[str] | None = None, *, bot: Telegram | None = None, path: Path | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    create = "--create" in raw
    args = [a for a in raw if a != "--create"]
    path = path or ROOT / "telegram.yaml"
    if bot is None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            print("TELEGRAM_BOT_TOKEN is not set (load .env first)")
            return 1
        bot = Telegram(token)
    try:
        # reading the updates does not mark them as read, so the review service loses nothing
        chat_id, found = rooms.plan(bot.get_updates(offset=None, timeout_s=0), title=args[0] if args else None)
        if create:
            for room in rooms.ROOMS:
                if room not in found:
                    found[room] = bot.create_forum_topic(chat_id, NAMES[room])
                    print(f"created the {NAMES[room]} topic")
    except (rooms.SetupError, TelegramError) as exc:
        print(f"could not set up the rooms: {exc}")
        print("Check: Topics are on, the bot is an admin with Manage topics, and you posted in each topic recently.")
        return 1
    before = rooms.load(path)
    rooms.write(path, chat_id, found)
    for room in found:
        if before.chat_id == chat_id and before.topics.get(room) == found[room]:
            continue  # connected on an earlier run: no second greeting
        hello = f"✅ The {room} room is connected."
        if room == "commands":
            hello += "\n\n" + help_text()
        try:
            bot.send_message(chat_id, hello, thread_id=found[room])
        except TelegramError as exc:
            print(f"wrote the file, but could not post in the {room} topic: {exc}")
    print(f"wrote {path}: group {chat_id}, topics {found}")
    missing = [room for room in rooms.ROOMS if room not in found]
    if missing:
        print(
            f"Still missing: {', '.join(missing)}. Post one message in each of those topics and run this again, "
            "or run it with --create to make new topics for them."
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
