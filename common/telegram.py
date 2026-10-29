"""A small Telegram Bot API client: send a video with buttons, read taps and exact commands, answer, edit a caption.

Telegram carries the bot token in the URL path, so this module takes care it never reaches an error message or a log:
HTTP errors are re-raised without their original (which holds the URL), and the HTTP library's request log is silenced.
Captions are plain text on purpose: no parse mode, so nothing in them can be read as formatting."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx2

TOKEN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{10,}")
CAPTION_MAX = 1024
TEXT_MAX = 4096
API = "https://api.telegram.org"

for _noisy in ("httpx2", "httpcore2"):  # their INFO/DEBUG lines print the request URL, which holds the token
    logging.getLogger(_noisy).setLevel(logging.WARNING)


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, *, base_url: str = API, timeout_s: float = 30) -> None:
        if not TOKEN.fullmatch(token):
            raise ValueError("not a valid Telegram bot token")
        self._base = f"{base_url.rstrip('/')}/bot{token}"
        self._timeout_s = timeout_s
        self._http = httpx2.Client(timeout=timeout_s)

    def _call(
        self, method: str, payload: dict[str, Any], *, files: dict[str, Any] | None = None, wait_s: float = 0
    ) -> Any:
        try:
            if files is None:
                reply = self._http.post(f"{self._base}/{method}", json=payload, timeout=self._timeout_s + wait_s)
            else:
                reply = self._http.post(
                    f"{self._base}/{method}", data=payload, files=files, timeout=self._timeout_s + wait_s
                )
        except httpx2.HTTPError as exc:
            raise TelegramError(f"{method}: {type(exc).__name__}") from None  # the original carries the URL
        try:
            body = reply.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {reply.status_code}") from None
        if not (isinstance(body, dict) and body.get("ok")):
            description = str(body.get("description", "")) if isinstance(body, dict) else ""
            raise TelegramError(f"{method}: {description or 'HTTP ' + str(reply.status_code)}")
        return body["result"]

    def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: list[list[tuple[str, str]]] | None = None,
        *,
        thread_id: int | None = None,
        html: bool = False,
    ) -> int:
        """A plain text message, with a row of (text, callback_data) buttons per list if given. `html=True` sends it as
        Telegram HTML: the caller has escaped every piece of text it did not write itself. Returns the message id."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": _fit(text, TEXT_MAX)}
        if html:
            payload["parse_mode"] = "HTML"
        if thread_id is not None:
            payload["message_thread_id"] = thread_id  # the topic of a forum group
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": _keyboard(buttons)}
        result = self._call("sendMessage", payload)
        return int(result["message_id"])

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None
    ) -> None:
        """Replace the text of a text message, and its buttons with the given ones (none: take them away)."""
        keyboard = _keyboard(buttons)
        self._call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": _fit(text, TEXT_MAX),
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )

    def send_video(
        self,
        chat_id: int,
        video: Path,
        caption: str,
        buttons: list[list[tuple[str, str]]],
        *,
        thread_id: int | None = None,
    ) -> int:
        """Send a video with a row of (text, callback_data) buttons per list. Returns the message id."""
        markup = {"inline_keyboard": _keyboard(buttons)}
        fields = {
            "chat_id": str(chat_id),
            "caption": _fit(caption),
            "supports_streaming": "true",
            "reply_markup": json.dumps(markup),
        }
        if thread_id is not None:
            fields["message_thread_id"] = str(thread_id)
        with video.open("rb") as handle:
            result = self._call("sendVideo", fields, files={"video": (video.name, handle, "video/mp4")})
        return int(result["message_id"])

    def send_document(self, chat_id: int, document: Path, caption: str, *, thread_id: int | None = None) -> int:
        """Send a file as it is (Telegram does not recompress a document). Returns the message id."""
        fields = {"chat_id": str(chat_id), "caption": _fit(caption), "disable_content_type_detection": "true"}
        if thread_id is not None:
            fields["message_thread_id"] = str(thread_id)
        with document.open("rb") as handle:
            result = self._call("sendDocument", fields, files={"document": (document.name, handle, "video/mp4")})
        return int(result["message_id"])

    def get_updates(self, *, offset: int | None, timeout_s: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout_s, "allowed_updates": ["callback_query", "message"]}
        if offset is not None:
            payload["offset"] = offset
        return list(self._call("getUpdates", payload, wait_s=timeout_s))

    def answer_callback(self, callback_id: str, text: str = "", *, alert: bool = False) -> None:
        """Answer a button tap. With `alert` the text is a popup that must be dismissed, not a toast that fades."""
        fields: dict[str, Any] = {"callback_query_id": callback_id, "text": text[:200]}
        if alert:
            fields["show_alert"] = True
        self._call("answerCallbackQuery", fields)

    def edit_caption(
        self, chat_id: int, message_id: int, caption: str, buttons: list[list[tuple[str, str]]] | None = None
    ) -> None:
        """Replace a message's caption, and its buttons with the given ones (none: take them away)."""
        keyboard = _keyboard(buttons)
        self._call(
            "editMessageCaption",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "caption": _fit(caption),
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )

    def create_forum_topic(self, chat_id: int, name: str) -> int:
        """Make a topic in a forum group (the bot needs the Manage topics right). Returns its thread id."""
        return int(self._call("createForumTopic", {"chat_id": chat_id, "name": name})["message_thread_id"])

    def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        """The list of commands Telegram shows when you type a slash."""
        self._call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})

    def close(self) -> None:
        self._http.close()


def _is_link(data: str) -> bool:
    """A web address to open (https, or this machine's own for tests), as opposed to the data of a tap."""
    return data.startswith("https://") or data.startswith(("http://127.0.0.1", "http://localhost"))


def _keyboard(buttons: list[list[tuple[str, str]]] | None) -> list[list[dict[str, str]]]:
    """Rows of (text, data): data that is a web address makes a button that opens it, anything else a tap."""
    return [
        [{"text": t, "url": d} if _is_link(d) else {"text": t, "callback_data": d} for t, d in row]
        for row in buttons or []
    ]


def _fit(text: str, limit: int = CAPTION_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"
