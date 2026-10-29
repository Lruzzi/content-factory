"""Reading the live chat of a YouTube stream. A reader hands back only WHEN each message came in (seconds since the
epoch): who wrote it and what it says are dropped here, so nothing personal ever reaches the database or a log."""

from __future__ import annotations

from typing import Protocol


class ChatReader(Protocol):
    def poll(self) -> list[float]:
        """The times of the messages that came in since the last call (empty when there are none)."""

    def alive(self) -> bool:
        """False once the chat is over, closed or unreachable."""

    def close(self) -> None: ...


class PytchatReader:
    """pytchat (MIT): reads the public chat the way the YouTube page does, with no sign-in. `interruptable=False`
    because it is used from a thread, and its signal handler only works in the main one."""

    def __init__(self, video_id: str) -> None:
        import pytchat  # noqa: PLC0415  # only this service needs it

        self._chat = pytchat.create(video_id=video_id, interruptable=False)

    def poll(self) -> list[float]:
        return [item.timestamp / 1000.0 for item in self._chat.get().sync_items()]

    def alive(self) -> bool:
        return bool(self._chat.is_alive())

    def close(self) -> None:
        self._chat.terminate()
