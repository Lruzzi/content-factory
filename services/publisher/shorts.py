"""Post an approved clip to the YouTube accounts of its profile (platform `shorts`): the shared rules and bookkeeping
are in base.py, this is what is specific to YouTube. The upload session is saved in the post row, so a repeat resumes
it; a finished session answers with the same video id."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from common import jobs
from common.media import LocalStore
from services.publisher import accounts as acc
from services.publisher import google_oauth as g
from services.publisher import posts
from services.publisher.base import AccountLimited, NeedsSignIn, Parked, PlatformPublisher, SendError, Sent
from services.publisher.youtube import SessionGone, UploadLimit, YouTube, YouTubeError


class ShortsPublisher(PlatformPublisher):
    platform = "shorts"
    text_key = "shorts"

    def __init__(
        self,
        engine: sa.Engine,
        store: LocalStore,
        accounts: acc.Accounts,
        uploader_for: Callable[[acc.AccountRules], YouTube],
        **kw: Any,
    ) -> None:
        super().__init__(engine, store, accounts, **kw)
        self.uploader_for = uploader_for

    def send(
        self,
        job: jobs.Job,
        rules: acc.AccountRules,
        post: posts.Post,
        file: Path,
        text: dict[str, Any],
        save_session: Callable[[str], None],
    ) -> Sent:
        try:
            uploaded = self.uploader_for(rules).upload(
                file,
                title=text["title"],
                description=text["text"],
                privacy=rules.visibility,
                session_url=post["session_url"],
                on_session=save_session,
            )
        except g.AuthExpired as exc:
            raise NeedsSignIn(str(exc)) from None
        except SessionGone:
            raise Parked(
                "the upload session was lost: check the channel for a duplicate before posting this clip again"
            ) from None
        except UploadLimit as exc:
            raise AccountLimited(str(exc)) from None
        except YouTubeError as exc:
            raise SendError(str(exc), retryable=exc.retryable) from None
        return Sent(uploaded.video_id, uploaded.url)
