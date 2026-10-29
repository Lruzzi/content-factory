"""The understand stage as a worker handler: transcribe a recorded segment and store it on the stream clock.

Everything about HOW comes from the job's `spec` snapshot. A job run twice finds its transcript and spends no
more Groq time. Any exception is recorded by the worker as a failed attempt.
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

import config_schema as cs
from common import jobs, ledger, pauses, spend
from common.media import LocalStore
from services.understand import audio, budget
from services.understand.transcribe import RateLimited, TranscribeUnavailable, Transcript

log = logging.getLogger(__name__)
SLACK_S = 5.0  # how much longer than its segment a transcript may claim to be

Transcriber = Callable[..., Transcript]  # (audio path, model=, language=, prompt=) -> Transcript


class UnderstandError(RuntimeError):
    pass


def make_handler(
    engine: Engine,
    store: LocalStore,
    transcribe: Transcriber,
    *,
    next_stage: str = "select",
    limits: budget.Limits | None = None,
    alert: Callable[[str], object] | None = None,
    plan: Callable[[sa.Connection, Any, float], budget.Wait | None] | None = None,
    fallback: Transcriber | None = None,
    spend_limits: spend.Limits | None = None,
) -> Callable[[jobs.Job], str]:
    def can_fall_back(fallback_engine: str, stream_id: Any) -> bool:
        """May a segment go to OpenRouter's Whisper instead of waiting for Groq? Only when the profile asks for it, the
        stream is still being recorded (a backlog of finished videos waits for Groq: paid, less accurate transcription
        is for the live stream) and the OpenRouter budget has not reached its stop."""
        if fallback is None or fallback_engine != "openrouter":
            return False
        with engine.connect() as conn:
            if stream_id is not None:
                status = conn.execute(
                    sa.text("SELECT status FROM live_streams WHERE id = :i"), {"i": str(stream_id)}
                ).scalar_one_or_none()
                if status == "ended":
                    return False
            return spend_limits is None or not spend.load(conn, spend_limits).stopped

    def tell_overflow(wait_s: float, reason: str) -> None:
        if alert is None:
            return
        with engine.begin() as conn:
            first = pauses.start(conn, "stt:overflow", wait_s, reason)
        if first:  # told once when it starts, not for every segment that goes the same way
            alert(
                f"Groq audio is nearly used up ({reason}), so segments are transcribed by OpenRouter's Whisper instead "
                "(about $0.012 per audio hour, from the OpenRouter budget). It is less accurate than Groq on slang, so "
                "clips from these hours may have more caption mistakes. It goes back to Groq when Groq has room again."
            )

    def handle(job: jobs.Job) -> str:
        profile = cs.Profile.model_validate(job["spec"])
        settings = profile.signals.transcript
        if not settings.enabled or settings.mode != "full":
            return next_stage  # only live segments of profiles that select from the full transcript are transcribed
        if settings.engine != "groq_api":
            raise UnderstandError(f"transcript engine {settings.engine!r} is not built yet")
        key, data = job["media_key"], job["data"] or {}
        with engine.connect() as conn:
            if conn.execute(sa.text("SELECT 1 FROM transcripts WHERE media_key = :k"), {"k": key}).first():
                return next_stage
        source = store.path(key)
        if not source.is_file():
            raise UnderstandError(f"segment {key!r} is not in the media store")
        expected = float(data["end_s"]) - float(data["start_s"]) if "end_s" in data and "start_s" in data else None
        wait: budget.Wait | None = None
        if limits is not None:  # sending this segment would pass a Groq limit: it overflows to OpenRouter, or waits
            with engine.connect() as conn:
                wait = budget.check(conn, limits, expected or 0)
        provider = "groq"
        if wait is not None:
            if can_fall_back(settings.fallback_engine, data.get("stream_id")):
                provider = "openrouter"
                tell_overflow(wait.wait_s, wait.reason)
            else:
                if alert is not None:  # told once when it starts, not for every segment that waits behind it
                    with engine.begin() as conn:
                        first = pauses.start(conn, "groq:audio", wait.wait_s, wait.reason)
                    if first:
                        alert(
                            f"Groq audio is nearly used up, so transcription is paused: {wait.reason}. Segments wait "
                            "and nothing is lost; recording carries on and retention keeps the files for now."
                        )
                raise jobs.Deferred(wait.wait_s, wait.reason)
        if plan is not None:  # the hours planner: this source's fair share of the day, and what the budget pays for
            with engine.connect() as conn:
                held = plan(conn, job["source_id"], expected or 0)
            if held is not None:
                raise jobs.Deferred(held.wait_s, held.reason)

        def run(transcriber: Transcriber, model: str) -> Transcript:
            with tempfile.TemporaryDirectory() as tmp:
                return transcriber(
                    audio.extract(source, Path(tmp) / "audio.ogg"),
                    model=model, language=profile.language, prompt=settings.prompt,
                )  # fmt: skip

        def overflow() -> Transcript | None:
            """The segment through OpenRouter's Whisper; None when it cannot answer (it then waits for Groq)."""
            if fallback is None:
                return None
            try:
                return run(fallback, settings.fallback_model)
            except (RateLimited, TranscribeUnavailable) as exc:
                log.warning("the OpenRouter transcription fallback could not answer: %s", exc)
                return None

        result: Transcript | None = None
        if provider == "openrouter":
            result = overflow()
            if result is None and wait is not None:
                raise jobs.Deferred(wait.wait_s, wait.reason)
        else:
            try:
                result = run(transcribe, settings.model)
            except RateLimited as limited:
                reason = f"Groq's rate limit was reached ({limited.reason}); waiting"
                if can_fall_back(settings.fallback_engine, data.get("stream_id")):
                    provider = "openrouter"
                    tell_overflow(limited.wait_s, limited.reason)
                    result = overflow()
                if result is None:
                    raise jobs.Deferred(limited.wait_s, reason) from None
        if result is None:  # not reachable: both paths above either set it or raise
            raise UnderstandError("no transcript was produced")
        if expected is not None and result.duration_s > expected + SLACK_S:
            raise UnderstandError(
                f"transcript duration {result.duration_s:.1f}s is longer than its {expected:.1f}s segment"
            )
        result = result.without_phrases(settings.ignore_phrases)
        shifted = result.shifted(float(data.get("start_s", 0.0)))
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO transcripts "
                    "(job_id, stream_id, media_key, start_s, duration_s, language, text, sentences, words) "
                    "VALUES (:job, :stream, :key, :start, :dur, :lang, :text, "
                    "CAST(:sentences AS jsonb), CAST(:words AS jsonb)) "
                    "ON CONFLICT (tenant_id, media_key) DO NOTHING"
                ),
                {
                    "job": job["id"], "stream": data.get("stream_id"), "key": key, "start": data.get("start_s", 0.0),
                    "dur": result.duration_s, "lang": result.language, "text": result.text,
                    "sentences": _json(shifted.sentences), "words": _json(shifted.words),
                },
            )  # fmt: skip
            ledger.record_cost(
                conn, profile=profile.name, service="understand", provider=provider,
                units=result.duration_s, unit="audio_s",
                usd=Decimal(str(result.cost_usd)) if provider == "openrouter" else Decimal(0), job_id=job["id"],
            )  # fmt: skip
        if limits is not None and alert is not None:
            with engine.connect() as conn:
                share = budget.day_share(conn, limits)
            if share >= limits.alert_at:
                level = int(share * 100) // 5 * 5  # in steps of 5%, so it is not repeated for every segment
                alert(f"Groq audio use is at {level}% of the daily limit (counted over the last 24 hours)")
        return next_stage

    return handle


def _json(spans: list[Any]) -> str:
    return json.dumps([s.model_dump() for s in spans])
