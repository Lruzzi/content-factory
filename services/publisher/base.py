"""What every platform publisher shares. For each account a clip is meant for (`Profile.upload_accounts()`), a `posts`
row is written before the platform is called and the platform's upload session is saved in it. A clip delivered again,
or a service restarted halfway, then resumes instead of posting twice. If a session cannot be verified, nobody can tell
whether the clip was posted, so the post is parked with an alert rather than risk a duplicate.

Daily cap, spacing and posting windows are checked before a post is started. A clip that may not go yet is left alone:
the service looks at waiting clips again every minute. The clip is finished when every account it was meant for, on
every platform, has a posted, failed or skipped row.

A platform subclass sets `platform` (the name accounts.yaml uses) and `text_key` (which of the package texts to send)
and implements `send`, which raises one of the four errors below to say what happened."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa

import config_schema as cs
from common import jobs, pauses
from common import rules as rules_mod
from common.media import LocalStore
from services.publisher import accounts as acc
from services.publisher import posts, schedule

log = logging.getLogger(__name__)
FINAL = ("posted", "failed", "skipped")


class SendError(Exception):
    """The platform refused or failed. `retryable` says whether trying again can help."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class NeedsSignIn(Exception):
    """The saved sign-in no longer works. The clip waits, nothing is lost, and you are told."""


class AccountLimited(Exception):
    """The platform says this account has posted enough for now (a daily upload limit, a spam rate limit). It says
    nothing is wrong with the clip: it waits, the account is left alone for a few hours, and you are told once."""


class Parked(Exception):
    """The upload cannot be verified, so a repeat could post twice: stop and ask a person."""


class Pending(Exception):
    """The platform is still processing the clip. Not a failure: it is looked at again on the next sweep."""


@dataclass(frozen=True)
class Sent:
    remote_id: str
    url: str | None


class PlatformPublisher:
    platform = ""
    text_key = ""

    def __init__(
        self,
        engine: sa.Engine,
        store: LocalStore,
        accounts: acc.Accounts,
        *,
        alert: Callable[[str], object] | None = None,
        now: Callable[[], datetime] | None = None,
        max_attempts: int = 3,
        pause_s: float = 1800,
        limit_pause_s: float = 6 * 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine, self.store, self.accounts = engine, store, accounts
        self.alert, self.now, self.max_attempts = alert or (lambda message: None), now, max_attempts
        self.pause_s, self.limit_pause_s, self.clock = pause_s, limit_pause_s, clock
        self._paused_until: dict[str, float] = {}  # accounts whose sign-in failed: left alone until this time

    def send(
        self,
        job: jobs.Job,
        rules: acc.AccountRules,
        post: posts.Post,
        file: Path,
        text: dict[str, Any],
        save_session: Callable[[str], None],
    ) -> Sent:
        raise NotImplementedError

    def process(self, job: jobs.Job) -> None:
        """Do whatever can be done now for this clip on this platform. Safe to call again at any time."""
        with self.engine.connect() as conn:
            if pauses.active(conn, pauses.switch_key("publish")) is not None:
                return  # posting is paused by hand: the clip waits, nothing is lost
        profile = cs.Profile.model_validate(job["spec"])
        targets = profile.upload_accounts()
        paused = self._source_paused(job)
        for account in targets:
            if self.accounts.by_id[account].platform == self.platform:
                self._post(job, account, paused)
        with self.engine.begin() as conn:
            if posts.all_final(conn, job["id"], targets):
                conn.execute(
                    sa.text(
                        "UPDATE jobs SET status = 'done', updated_at = clock_timestamp() "
                        "WHERE id = :i AND stage = 'publish' AND status = 'queued'"
                    ),
                    {"i": job["id"]},
                )

    def _source_paused(self, job: jobs.Job) -> bool:
        if job["source_id"] is None:
            return False
        with self.engine.connect() as conn:
            return bool(
                conn.execute(
                    sa.text("SELECT paused_at IS NOT NULL FROM sources WHERE id = :i"), {"i": job["source_id"]}
                ).scalar_one_or_none()
            )

    def _post(self, job: jobs.Job, account: str, paused: bool) -> None:
        with self.engine.begin() as conn:
            rules = rules_mod.apply(conn, self.accounts.by_id[account])  # what you set from Telegram wins
            post = posts.existing(conn, job["id"], account)
            if post is not None and post["state"] in FINAL:
                return
            if paused:
                posts.skip(conn, job["id"], account, self.platform, "the source is paused")
                return
            if post is None:
                if self.clock() < self._paused_until.get(account, 0.0):
                    return  # paused (sign-in, posting limit): a post started now would only use up its cap and spacing
                waiting = schedule.check(
                    now=self.now() if self.now else conn.execute(sa.text("SELECT clock_timestamp()")).scalar_one(),
                    posts=posts.recent_times(conn, account),
                    cap=rules.daily_cap,
                    spacing_min=rules.min_spacing_min,
                    windows=rules.post_windows,
                    tz=self.accounts.timezone,
                )
                if waiting:
                    log.info("clip %s waits for %s: %s", job["id"], account, waiting)
                    return
                post, _ = posts.claim(conn, job["id"], account, self.platform)
        self._upload(job, account, rules, post)

    def _fail(self, job: jobs.Job, account: str, post: posts.Post, error: str, *, final: bool) -> None:
        with self.engine.begin() as conn:
            state = "failed"
            if final:
                posts.fail_now(conn, post["id"], error)
            else:
                state = posts.record_failure(conn, post["id"], error, max_attempts=self.max_attempts)
        if state == "failed":
            self.alert(f"post of job {job['id']} to {account} failed: {error}")

    def _upload(self, job: jobs.Job, account: str, rules: acc.AccountRules, post: posts.Post) -> None:
        data: dict[str, Any] = job["data"] or {}
        file = self.store.path(data["render"]["output"]["key"])
        if not file.is_file():
            return self._fail(job, account, post, f"the clip {file.name!r} is not in the media store", final=True)

        if self.clock() < self._paused_until.get(account, 0.0):
            return  # its sign-in failed a moment ago: asking a blocked platform again every minute only makes it worse

        def save_session(session: str) -> None:
            with self.engine.begin() as conn:
                posts.record_session(conn, post["id"], session)

        try:
            sent = self.send(job, rules, post, file, data["package"][self.text_key], save_session)
        except NeedsSignIn as exc:
            self._paused_until[account] = self.clock() + self.pause_s
            self.alert(f"cannot post to {account}: {exc}")  # the clip waits; nothing is lost
            return
        except AccountLimited as exc:
            self._paused_until[account] = self.clock() + self.limit_pause_s
            self.alert(f"{account} is at a posting limit, so its clips wait {self.limit_pause_s / 3600:g} h: {exc}")
            return
        except Pending:
            return
        except Parked as exc:
            return self._fail(job, account, post, str(exc), final=True)
        except SendError as exc:
            return self._fail(job, account, post, str(exc), final=not exc.retryable)
        with self.engine.begin() as conn:
            posts.mark_posted(conn, post["id"], sent.remote_id, sent.url or "")
        log.info("clip %s posted to %s: %s", job["id"], account, sent.url or sent.remote_id)
