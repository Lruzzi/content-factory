"""Which model's clips get approved: the numbers behind choosing an LLM or a transcription engine by what you tapped."""

from __future__ import annotations

import pytest

import config_schema as cs
from common import jobs, sources
from services.review import scores

pytestmark = pytest.mark.integration


def clip(db, llm, state, stt="groq_api/whisper-large-v3-turbo", kind="highlight"):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        profile = next(p for p in profiles if p.source.id == "leokabuki")
        data = {"clip": {"kind": kind, "llm": llm, "stt": stt}}
        if state:
            data["review"] = {"state": state}
        jobs.create_job(conn, profile, stage="review", kind=kind, data=data)


def test_each_model_gets_its_approved_rejected_and_waiting_counts_and_its_approval_rate(db):
    for state in ("approved", "approved", "approved", "rejected"):
        clip(db, "openai_compatible/gemini-3.5-flash", state)
    for state in ("approved", "rejected", "rejected", "expired", "pending"):
        clip(db, "openai_compatible/recursive/flint:free", state)
    with db.connect() as conn:
        rows = {r.llm: r for r in scores.by_model(conn)}
    gemini, flint = rows["openai_compatible/gemini-3.5-flash"], rows["openai_compatible/recursive/flint:free"]
    assert (gemini.approved, gemini.rejected, gemini.waiting) == (3, 1, 0) and gemini.rate == pytest.approx(0.75)
    assert (flint.approved, flint.rejected, flint.waiting) == (1, 3, 1) and flint.rate == pytest.approx(
        0.25
    )  # expired = no


def test_a_model_with_nothing_decided_yet_has_no_rate(db):
    clip(db, "x/y", "pending")
    with db.connect() as conn:
        (row,) = scores.by_model(conn)
    assert row.rate is None


def test_clips_made_before_the_model_was_recorded_are_grouped_as_unknown(db):
    clip(db, None, "approved")
    with db.connect() as conn:
        (row,) = scores.by_model(conn)
    assert row.llm == "unknown"


def test_transcription_engines_are_counted_the_same_way(db):
    clip(db, "a/b", "approved", stt="groq_api/whisper-large-v3-turbo")
    clip(db, "a/b", "rejected", stt="groq_api/whisper-large-v3")
    with db.connect() as conn:
        rows = {r.stt: r for r in scores.by_stt(conn)}
    assert rows["groq_api/whisper-large-v3-turbo"].approved == 1 and rows["groq_api/whisper-large-v3"].rejected == 1


def test_the_report_is_readable_text_with_a_line_per_model(db):
    clip(db, "a/b", "approved")
    clip(db, "a/b", "rejected")
    with db.connect() as conn:
        text = scores.report(conn)
    assert "a/b" in text and "50%" in text and "approved" in text.lower()


def rejected(db, llm, reason):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        profile = next(p for p in profiles if p.source.id == "leokabuki")
        review = {"state": "rejected", **({"reason": reason} if reason else {})}
        jobs.create_job(
            conn, profile, stage="review", kind="highlight",
            data={"clip": {"kind": "highlight", "llm": llm, "stt": "groq_api/x"}, "review": review},
        )  # fmt: skip


def test_the_report_counts_why_each_models_clips_were_rejected(db):
    for reason in ("boring", "boring", "layout", None):
        rejected(db, "x/model-a", reason)
    rejected(db, "x/model-b", "cut")
    with db.connect() as conn:
        counts = {(r.llm, r.reason): r.count for r in scores.reject_reasons(conn)}
        text = scores.report(conn)
    assert counts == {
        ("x/model-a", "boring"): 2, ("x/model-a", "layout"): 1, ("x/model-a", "none given"): 1,
        ("x/model-b", "cut"): 1,
    }  # fmt: skip
    assert "Why clips were rejected" in text and "boring 2" in text
