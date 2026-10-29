"""What the Facebook and Instagram publishers share: credentials read from the environment variables an account names,
a short wait for the platform to process a clip, and the translation of Meta errors into the publisher's outcomes."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import sqlalchemy as sa

from common.media import LocalStore
from services.publisher import accounts as acc
from services.publisher.base import AccountLimited, NeedsSignIn, PlatformPublisher, SendError
from services.publisher.meta import Meta, MetaError, MetaLimit, MetaTokenError


class MetaPublisher(PlatformPublisher):
    def __init__(
        self,
        engine: sa.Engine,
        store: LocalStore,
        accounts: acc.Accounts,
        meta_for: Callable[[acc.AccountRules], Meta],
        *,
        env: Mapping[str, str],
        poll_s: float = 5,
        polls: int = 12,
        sleep: Callable[[float], None] = time.sleep,
        **kw: Any,
    ) -> None:
        super().__init__(engine, store, accounts, **kw)
        self.meta_for, self.env, self.poll_s, self.polls, self.sleep = meta_for, env, poll_s, polls, sleep

    def _cred(self, rules: acc.AccountRules, key: str) -> str:
        """The value of the environment variable that the account's credentials name under `key`."""
        name = rules.credentials.get(key)
        value = self.env.get(name or "")
        if not value:
            raise NeedsSignIn(f"{rules.id} has no value for {name or key}: set it in .env (make meta-tokens writes it)")
        return value

    def _wait_until(self, ready: Callable[[], bool]) -> bool:
        """Check up to `polls` times, `poll_s` apart. False if it was still not ready."""
        for attempt in range(self.polls):
            if ready():
                return True
            if attempt + 1 < self.polls:
                self.sleep(self.poll_s)
        return False

    @staticmethod
    def _translate(exc: MetaError, account: str) -> Exception:
        if isinstance(exc, MetaTokenError):
            return NeedsSignIn(
                f"Meta no longer accepts the token of {account}: make a new one with make meta-tokens ({exc})"
            )
        if isinstance(exc, MetaLimit):
            return AccountLimited(str(exc))
        return SendError(str(exc), retryable=exc.retryable)
