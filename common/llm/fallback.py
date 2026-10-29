"""An ordered chain of LLMs: the first that answers wins.

A provider that fails (rate limit, outage, rejected key, a model that no longer exists) hands the call to the next one,
and is skipped for `cooldown_s` so a dead provider is not hammered. When every provider is cooling down they are all
tried anyway, primary first. Backups are built only when needed, so a missing backup key never breaks a working
primary. A switch is announced through `notify` (it is the caller's job to tell the operator).
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from common.llm.base import LLMClient, LLMError, LLMResult


@dataclass(frozen=True)
class Link:
    label: str
    build: Callable[[], LLMClient]


class FallbackClient:
    def __init__(
        self,
        links: Sequence[Link],
        *,
        cooldown_s: float,
        notify: Callable[[str], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not links:
            raise ValueError("a fallback chain needs at least one link")
        self._links, self._cooldown_s, self._notify, self._clock = list(links), cooldown_s, notify, clock
        self._clients: dict[int, LLMClient] = {}
        self._failed_at: dict[int, float] = {}

    def __repr__(self) -> str:
        return f"FallbackClient({[link.label for link in self._links]})"

    def complete(self, *, system: str, prompt: str, max_tokens: int) -> LLMResult:
        failures: list[tuple[str, LLMError]] = []
        for index in self._order():
            link = self._links[index]
            try:
                result = self._client(index).complete(system=system, prompt=prompt, max_tokens=max_tokens)
            except LLMError as exc:
                self._failed_at[index] = self._clock()
                failures.append((link.label, exc))
                continue
            self._failed_at.pop(index, None)
            if failures:
                self._announce(failures, link.label)
            return result
        detail = "; ".join(f"{label}: {' '.join(str(exc).split())}" for label, exc in failures)
        limited = all(exc.rate_limited for _, exc in failures)  # only a quota problem if none of them is broken
        hints = [exc.retry_after_s for _, exc in failures if exc.retry_after_s is not None]
        raise LLMError(
            f"every LLM provider failed: {detail}",
            retryable=any(exc.retryable for _, exc in failures),
            rate_limited=limited,
            retry_after_s=min(hints) if limited and hints else None,
        )

    def _order(self) -> list[int]:
        """Providers not cooling down first, in order, then the ones that are (a last resort)."""
        now = self._clock()
        ready = [i for i in range(len(self._links)) if now - self._failed_at.get(i, -1e18) >= self._cooldown_s]
        return ready + [i for i in range(len(self._links)) if i not in ready]

    def _client(self, index: int) -> LLMClient:
        if index not in self._clients:
            self._clients[index] = self._links[index].build()
        return self._clients[index]

    def _announce(self, failures: list[tuple[str, LLMError]], served_by: str) -> None:
        if self._notify is None:
            return
        failed = ", ".join(f"{label} ({' '.join(str(exc).split())[:160]})" for label, exc in failures)
        with contextlib.suppress(Exception):  # an alert that cannot be sent must not fail the call
            self._notify(f"LLM fallback: {failed} failed; {served_by} answered instead")
