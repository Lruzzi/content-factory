"""The understand stage as a worker handler: transcribe a recorded segment, store it on the stream clock (FR-3)."""

from __future__ import annotations

import shutil
from decimal import Decimal

import pytest
import sqlalchemy as sa
from media_helpers import make_clip

import config_schema as cs
from common import jobs, sources
from common.media import LocalStore
from services.understand import handler as understand
from services.understand import transcribe

REPLY = {
    "language": "Indonesian",
    "duration": 20.0,
    "text": " halo guys",
    "segments": [{"start": 0.0, "end": 6.0, "text": " halo guys"}],
    "words": [{"word": "halo", "start": 0.0, "end": 0.5}, {"word": "guys", "start": 0.5, "end": 1.0}],
}


class FakeGroq:
    def __init__(self, reply=REPLY, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def __call__(self, path, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return transcribe.parse(self.reply)


@pytest.fixture
def media(tmp_path):
    store = LocalStore(tmp_path / "media")
    key = "segments/s1/00001.ts"
    path = store.path(key)
    path.parent.mkdir(parents=True)
    make_clip(path)
    return store, key


def make_job(db, key, *, mode="full", start_s=240.0):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        profile = next(p for p in profiles if p.source.id == "leokabuki")
        profile.signals.transcript.mode = mode
        if mode != "full":
            profile.selection.mode = "signals"  # hybrid selection needs the full transcript
            profile.compilation.enabled = False  # so does a recap
        job_id = jobs.create_job(
            conn, profile, stage="understand", media_key=key,
            data={"live": True, "stream_id": None, "video_id": "vid00001", "segment": 1,
                  "start_s": start_s, "end_s": start_s + 20},
        )  # fmt: skip
        return jobs.get(conn, job_id)


def rows(db):
    with db.connect() as conn:
        return conn.execute(sa.text("SELECT * FROM transcripts")).mappings().all()


pytestmark = [pytest.mark.integration, pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")]


def test_a_segment_is_transcribed_and_stored_on_the_stream_clock(db, media):
    store, key = media
    groq = FakeGroq()
    job = make_job(db, key)
    assert understand.make_handler(db, store, groq)(job) == "select"
    (row,) = rows(db)
    assert row["media_key"] == key and row["job_id"] == job["id"] and row["language"] == "Indonesian"
    assert float(row["start_s"]) == 240.0 and float(row["duration_s"]) == 20.0
    assert row["sentences"] == [{"text": "halo guys", "start_s": 240.0, "end_s": 246.0}]
    assert row["words"][1] == {"text": "guys", "start_s": 240.5, "end_s": 241.0}
    configured = job["spec"]["signals"]["transcript"]["prompt"]
    assert configured  # mlbb.yaml gives the mixed Indonesian-English vocabulary
    assert groq.calls == [{"model": "whisper-large-v3-turbo", "language": "id", "prompt": configured}]


def test_the_profiles_vocabulary_prompt_is_sent_with_the_audio(db, media):
    store, key = media
    groq = FakeGroq()
    job = make_job(db, key)
    job["spec"]["signals"]["transcript"]["prompt"] = "Halo guys, push rank Mythic Glory, savage dan maniac."
    understand.make_handler(db, store, groq)(job)
    assert groq.calls[0]["prompt"] == "Halo guys, push rank Mythic Glory, savage dan maniac."


def test_the_audio_seconds_are_recorded_in_the_ledger_as_free_groq_usage(db, media):
    store, key = media
    understand.make_handler(db, store, FakeGroq())(make_job(db, key))
    with db.connect() as conn:
        cost = conn.execute(sa.text("SELECT * FROM costs")).mappings().one()
    assert (cost["service"], cost["provider"], cost["unit"]) == ("understand", "groq", "audio_s")
    assert float(cost["units"]) == 20.0 and float(cost["usd"]) == 0.0


def test_running_a_job_twice_keeps_one_transcript_and_does_not_send_the_audio_again(db, media):
    store, key = media
    groq = FakeGroq()
    handle = understand.make_handler(db, store, groq)
    job = make_job(db, key)
    handle(job)
    assert handle(job) == "select"
    assert len(rows(db)) == 1 and len(groq.calls) == 1  # the second run found the transcript and spent no Groq time


def test_a_profile_that_does_not_transcribe_everything_skips_the_call(db, media):
    store, key = media
    groq = FakeGroq()
    assert understand.make_handler(db, store, groq)(make_job(db, key, mode="candidates_only")) == "select"
    assert groq.calls == [] and rows(db) == []


def test_a_failed_transcription_stores_nothing_and_the_error_reaches_the_worker(db, media):
    store, key = media
    handle = understand.make_handler(db, store, FakeGroq(error=RuntimeError("429 rate limited")))
    with pytest.raises(RuntimeError, match="429"):
        handle(make_job(db, key))
    assert rows(db) == []


def test_a_segment_missing_from_the_store_is_an_error(db, tmp_path):
    with pytest.raises(understand.UnderstandError, match="not in the media store"):
        understand.make_handler(db, LocalStore(tmp_path / "empty"), FakeGroq())(make_job(db, "segments/s1/00009.ts"))


def test_a_transcript_longer_than_its_segment_is_rejected(db, media):
    store, key = media
    too_long = REPLY | {"duration": 90.0, "words": []}
    with pytest.raises(understand.UnderstandError, match="duration"):
        understand.make_handler(db, store, FakeGroq(too_long))(make_job(db, key))


# ---------- the hours planner ----------
def test_a_segment_the_planner_holds_back_waits_without_calling_groq_or_using_an_attempt(db, media):
    from services.understand.budget import Wait

    store, key = media
    groq = FakeGroq()
    job = make_job(db, key)
    asked: list[tuple] = []

    def plan(conn, source_id, needed_s):
        asked.append((source_id, needed_s))
        return Wait(600, "this source has used its fair share")

    with pytest.raises(jobs.Deferred) as caught:
        understand.make_handler(db, store, groq, plan=plan)(job)
    assert caught.value.wait_s == 600 and "fair share" in caught.value.reason
    assert groq.calls == [] and rows(db) == []
    assert asked == [(job["source_id"], 20.0)]  # who is asking, and how many seconds of audio it would send


def test_a_segment_the_planner_lets_through_is_transcribed_as_usual(db, media):
    store, key = media
    groq = FakeGroq()
    assert (
        understand.make_handler(db, store, groq, plan=lambda conn, source_id, needed_s: None)(make_job(db, key))
        == "select"
    )
    assert len(groq.calls) == 1


# ---------- Groq is nearly full: the segment goes to OpenRouter's Whisper instead of waiting ----------
from common import spend  # noqa: E402
from services.understand import budget as groq_budget  # noqa: E402

FULL = groq_budget.Limits(
    audio_seconds_per_hour=7200, audio_seconds_per_day=10, alert_at=0.8, stop_at=0.9
)  # any segment waits
ROOM = groq_budget.Limits(audio_seconds_per_hour=7200, audio_seconds_per_day=28800, alert_at=0.8, stop_at=0.9)
SPEND = spend.Limits(cap_ceiling_usd=100.0, cap_usd=6.0, alert_at=0.8, stop_at=0.9)


class FakeOpenRouter(FakeGroq):
    def __init__(self, reply=REPLY | {"usage": {"seconds": 20.0, "cost": 6.66e-05}}, error=None):
        super().__init__(reply, error)

    def __call__(self, path, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        t = transcribe.parse(self.reply)
        return t.model_copy(update={"cost_usd": float(self.reply.get("usage", {}).get("cost", 0))})


def overflow(db, media, *, groq=None, fallback=None, limits=FULL, alerts=None, engine="openrouter", spend_limits=SPEND):
    store, key = media
    job = make_job(db, key)
    job["spec"]["signals"]["transcript"]["fallback_engine"] = engine
    handler = understand.make_handler(
        db, store, groq or FakeGroq(), limits=limits, fallback=fallback,
        alert=(alerts.append if alerts is not None else None), spend_limits=spend_limits,
    )  # fmt: skip
    return handler, job


def costs(db):
    with db.connect() as conn:
        return conn.execute(sa.text("SELECT provider, unit, units, usd FROM costs ORDER BY id")).mappings().all()


def test_a_segment_that_would_pass_the_groq_stop_is_transcribed_by_openrouter_and_paid_for(db, media):
    groq, fallback, alerts = FakeGroq(), FakeOpenRouter(), []
    handler, job = overflow(db, media, groq=groq, fallback=fallback, alerts=alerts)
    assert handler(job) == "select"
    assert groq.calls == [] and len(fallback.calls) == 1
    assert fallback.calls[0]["model"] == "openai/whisper-large-v3-turbo" and fallback.calls[0]["language"] == "id"
    (row,) = costs(db)
    assert (row["provider"], row["unit"], float(row["units"])) == ("openrouter", "audio_s", 20.0)
    assert float(row["usd"]) == pytest.approx(6.66e-05, abs=1e-6)  # the ledger keeps millionths of a dollar
    (stored,) = rows(db)
    assert stored["media_key"] == job["media_key"] and float(stored["start_s"]) == 240.0
    assert len(alerts) == 1 and "OpenRouter" in alerts[0]


def test_the_overflow_is_announced_once_not_for_every_segment(db, media):
    alerts: list[str] = []
    store, key = media
    shutil.copy(store.path(key), store.path("segments/s1/00002.ts"))
    for media_key in (key, "segments/s1/00002.ts"):
        job = make_job(db, media_key)
        job["spec"]["signals"]["transcript"]["fallback_engine"] = "openrouter"
        understand.make_handler(
            db, store, FakeGroq(), limits=FULL, fallback=FakeOpenRouter(), alert=alerts.append, spend_limits=SPEND
        )(job)
    assert len(alerts) == 1 and len(rows(db)) == 2


def test_with_room_on_groq_the_fallback_is_not_touched(db, media):
    groq, fallback = FakeGroq(), FakeOpenRouter()
    handler, job = overflow(db, media, groq=groq, fallback=fallback, limits=ROOM)
    handler(job)
    assert len(groq.calls) == 1 and fallback.calls == []
    assert [r["provider"] for r in costs(db)] == ["groq"]


def test_a_groq_rate_limit_during_the_call_also_goes_to_the_fallback(db, media):
    groq = FakeGroq(error=transcribe.RateLimited(60, "the answer was a 429"))
    fallback = FakeOpenRouter()
    handler, job = overflow(db, media, groq=groq, fallback=fallback, limits=ROOM)
    assert handler(job) == "select" and len(fallback.calls) == 1


def test_without_the_openrouter_fallback_in_the_profile_the_segment_waits_as_before(db, media):
    fallback = FakeOpenRouter()
    handler, job = overflow(db, media, fallback=fallback, engine="none")
    with pytest.raises(jobs.Deferred):
        handler(job)
    assert fallback.calls == [] and rows(db) == []


def test_without_a_fallback_transcriber_the_segment_waits_as_before(db, media):
    handler, job = overflow(db, media, fallback=None)
    with pytest.raises(jobs.Deferred):
        handler(job)


def test_when_the_openrouter_budget_is_stopped_the_segment_waits_instead_of_spending(db, media):
    with db.begin() as conn:
        from common import ledger

        ledger.record_cost(
            conn,
            profile="mlbb",
            service="select",
            provider="openai_compatible",
            units=1,
            unit="input_tokens",
            usd=Decimal("5.8"),
        )  # 97% of the 6 dollar cap: past the stop
    fallback = FakeOpenRouter()
    handler, job = overflow(db, media, fallback=fallback)
    with pytest.raises(jobs.Deferred):
        handler(job)
    assert fallback.calls == []


def test_a_fallback_that_cannot_answer_leaves_the_segment_waiting_for_groq(db, media):
    fallback = FakeOpenRouter(error=transcribe.TranscribeUnavailable("OpenRouter returned 402"))
    handler, job = overflow(db, media, fallback=fallback)
    with pytest.raises(jobs.Deferred) as caught:
        handler(job)
    assert "Groq" in caught.value.reason and rows(db) == []


def test_a_fallback_that_is_rate_limited_also_leaves_the_segment_waiting(db, media):
    fallback = FakeOpenRouter(error=transcribe.RateLimited(60, "the answer was a 429"))
    handler, job = overflow(db, media, fallback=fallback)
    with pytest.raises(jobs.Deferred):
        handler(job)


def stream_row(db, status):
    with db.begin() as conn:
        return conn.execute(
            sa.text("INSERT INTO live_streams (video_id, status) VALUES (:v, :s) RETURNING id"),
            {"v": f"vid{status}001", "s": status},
        ).scalar_one()


def test_a_segment_of_the_stream_being_recorded_may_overflow_to_openrouter(db, media):
    live = stream_row(db, "live")
    fallback = FakeOpenRouter()
    handler, job = overflow(db, media, fallback=fallback)
    job["data"]["stream_id"] = str(live)
    assert handler(job) == "select" and len(fallback.calls) == 1


def test_a_segment_of_a_finished_stream_waits_for_groq_instead_of_spending_credit(db, media):
    """A backlog of finished videos (discover's backfill) is not worth paid, less accurate transcription: the live
    stream is what needs Groq's room and, when that is full, the paid overflow."""
    ended = stream_row(db, "ended")
    fallback = FakeOpenRouter()
    handler, job = overflow(db, media, fallback=fallback)
    job["data"]["stream_id"] = str(ended)
    with pytest.raises(jobs.Deferred):
        handler(job)
    assert fallback.calls == [] and rows(db) == []
