"""Four rooms in one Telegram group (its topics): `approval` (clip previews and the TikTok files to post), `alerts`
(what needs a person), `monitoring` (service health and a periodic digest) and `commands` (where you type). The group
and topic ids are in config/telegram.yaml (not secrets). Without a group everything goes to your private chat."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOMS = ("approval", "alerts", "monitoring", "commands")
KEYWORDS = {  # what a topic's name has to contain for it to be taken for that room
    "approval": ("approv",),
    "alerts": ("alert", "warn", "error"),
    "monitoring": ("monitor",),
    "commands": ("command", "cmd"),
}


class SetupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Destination:
    chat_id: int
    thread_id: int | None = None  # the topic; None is a private chat or the group's General topic


@dataclass(frozen=True)
class Rooms:
    chat_id: int | None
    topics: dict[str, int]

    @property
    def enabled(self) -> bool:
        return self.chat_id is not None

    def where(self, room: str, users: list[int]) -> list[Destination]:
        """Where messages of this kind go: its topic in the group, or each allowed user's private chat."""
        if self.chat_id is None:
            return [Destination(user) for user in users]
        return [Destination(self.chat_id, self.topics.get(room))]

    def is_commands_room(self, chat_id: object, thread_id: object) -> bool:
        topic = self.topics.get("commands")
        return self.chat_id is not None and chat_id == self.chat_id and topic is not None and thread_id == topic


def _bad(why: str) -> ValueError:
    return ValueError(f"telegram.yaml: {why}")


def load(path: Path) -> Rooms:
    """The group and topic ids from the file; no file means no group."""
    path = Path(path)
    if not path.is_file():
        return Rooms(None, {})
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or set(raw) - {"chat_id", "topics"}:
        raise _bad("only chat_id and topics are allowed")
    chat = raw.get("chat_id")
    if chat is not None and (isinstance(chat, bool) or not isinstance(chat, int) or chat >= 0):
        raise _bad("chat_id must be the group's id, a negative number (or null)")
    topics_raw = raw.get("topics") or {}
    if not isinstance(topics_raw, dict) or set(topics_raw) - set(ROOMS):
        raise _bad(f"topics may only name these rooms: {', '.join(ROOMS)}")
    topics: dict[str, int] = {}
    for room, thread in topics_raw.items():
        if thread is None:
            continue
        if isinstance(thread, bool) or not isinstance(thread, int) or thread <= 0:
            raise _bad(f"the topic id of {room} must be a positive number (or null)")
        topics[room] = thread
    return Rooms(chat, topics)


def from_config() -> Rooms:
    from config_schema import ROOT

    return load(ROOT / "telegram.yaml")


def _names(update: dict[str, Any]) -> list[tuple[int, str, int, str]]:
    """(group id, group title, thread id, topic name) found in one update."""
    message = update.get("message")
    if not isinstance(message, dict):
        return []
    chat = message.get("chat") or {}
    if chat.get("type") != "supergroup" or not chat.get("is_forum"):
        return []
    found = []
    created = message.get("forum_topic_created")
    if isinstance(created, dict):
        found.append(
            (
                chat["id"],
                str(chat.get("title", "")),
                message.get("message_thread_id") or message["message_id"],
                str(created.get("name", "")),
            )
        )
    root = (message.get("reply_to_message") or {}).get("forum_topic_created")
    if isinstance(root, dict) and message.get("message_thread_id"):
        found.append((chat["id"], str(chat.get("title", "")), message["message_thread_id"], str(root.get("name", ""))))
    if not found and chat.get("id") is not None:
        found.append((chat["id"], str(chat.get("title", "")), 0, ""))  # a forum group with no topic name learned yet
    return found


def plan(updates: list[dict[str, Any]], *, title: str | None = None) -> tuple[int, dict[str, int]]:
    """From the bot's recent updates: the forum group and which topic is which room. A topic is known from its creation
    message or from any message you posted in it."""
    groups: dict[int, tuple[str, dict[int, str]]] = {}
    for update in updates:
        for chat_id, chat_title, thread, name in _names(update):
            _, topics = groups.setdefault(chat_id, (chat_title, {}))
            if thread and name:
                topics[thread] = name
    if title:
        groups = {k: v for k, v in groups.items() if title.casefold() in v[0].casefold()}
    if not groups:
        raise SetupError("no group with topics turned on was found in the bot's recent messages")
    if len(groups) > 1:
        raise SetupError("more than one group was found: " + ", ".join(f"{v[0]} ({k})" for k, v in groups.items()))
    ((chat_id, (_, names)),) = groups.items()
    found: dict[str, int] = {}
    for thread, name in sorted(names.items()):
        for room in ROOMS:
            if room not in found and any(word in name.casefold() for word in KEYWORDS[room]):
                found[room] = thread
                break
    return chat_id, found


def write(path: Path, chat_id: int, topics: dict[str, int]) -> None:
    header = (
        "# The Telegram group and its four topics (rooms). Not secrets.\n"
        "# `python -m services.review.rooms_setup` fills this in.\n"
        "# Without chat_id everything goes to your private chat.\n"
    )
    body = yaml.safe_dump({"chat_id": chat_id, "topics": {room: topics.get(room) for room in ROOMS}}, sort_keys=False)
    Path(path).write_text(header + body, encoding="utf-8")
