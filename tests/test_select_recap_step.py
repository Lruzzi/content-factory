"""After a segment is transcribed: find finished matches in the recent transcript, one recap job per new match."""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import jobs, sources
from common.llm.base import LLMResult
from services.select import recap_step as step

pytestmark = pytest.mark.integration

PRICES = {"fake/model": cs.Price(input=0.1, output=0.4)}
STREAM = "00000000-0000-0000-0000-0000000000bb"
SEG_S = 600.0


def match_reply(*matches):
    return json.dumps(
        {
            "matches": [
                {"start_sentence": a, "end_sentence": b, "summary": "satu match", "ended": e} for a, b, e in matches
            ]
        }
    )


def recap_reply(*moments, title="Recap seru"):
    return json.dumps(
        {
            "title": title,
            "moments": [{"start_sentence": a, "end_sentence": b, "score": s, "reason": "seru"} for a, b, s in moments],
        }
    )


class FakeLLM:
    def __init__(self, matches=None, recap=None):
        self.matches = matches or match_reply((20, 199, True))
        self.recap = recap or recap_reply((30, 36, 0.9), (100, 106, 0.8), (150, 156, 0.7), (190, 196, 0.6))
        self.calls: list[str] = []

    def complete(self, *, system, prompt, max_tokens):
        kind = "recap" if "Pick the best moments" in prompt else "match"
        self.calls.append(kind)
        return LLMResult(self.recap if kind == "recap" else self.matches, 500, 100, "fake", price_key="fake/model")


@pytest.fixture
def world(db):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        profile = next(p for p in profiles if p.source.id == "leokabuki")
        profile.selection.trim = None  # the fixtures have one word to a sentence: cut at the sentence times here
        profile.signals.transcript.glossary = []  # no extra model question here: the tests count the calls
        source_id = conn.execute(sa.text("SELECT id FROM sources WHERE source_key = 'leokabuki'")).scalar_one()
        conn.execute(
            sa.text("INSERT INTO live_streams (id, video_id, source_id) VALUES (:i, 'vid00001', :s)"),
            {"i": STREAM, "s": source_id},
        )
        for index in range(3):
            start = index * SEG_S
            sentences = [
                {"text": f"kalimat {k}", "start_s": start + k * 5.0, "end_s": start + (k + 1) * 5.0} for k in range(120)
            ]
            words = [
                {"text": f"w{index * 120 + k}", "start_s": start + k * 5.0, "end_s": start + k * 5.0 + 1.0}
                for k in range(120)
            ]
            key = f"segments/{STREAM}/{index:05d}.ts"
            conn.execute(
                sa.text(
                    "INSERT INTO transcripts "
                    "(stream_id, media_key, start_s, duration_s, language, text, sentences, words) "
                    "VALUES (:s, :k, :a, 600, 'Indonesian', 't', CAST(:se AS jsonb), CAST(:w AS jsonb))"
                ),
                {"s": STREAM, "k": key, "a": start, "se": json.dumps(sentences), "w": json.dumps(words)},
            )
            conn.execute(
                sa.text("INSERT INTO segments (stream_id, idx, key, start_s, end_s) VALUES (:s, :i, :k, :a, :b)"),
                {"s": STREAM, "i": index, "k": key, "a": start, "b": start + SEG_S},
            )
        job_id = jobs.create_job(
            conn, profile, stage="select", source_id=source_id, media_key=f"segments/{STREAM}/00002.ts",
            data={
                "live": True, "stream_id": STREAM, "video_id": "vid00001",
                "segment": 2, "start_s": 1200.0, "end_s": 1800.0,
            },
        )  # fmt: skip
        return {"job": jobs.get(conn, job_id), "profile": profile}


def run(db, world, llm, published=None, profile=None):
    s = step.RecapStep(
        db, lambda spec: llm, PRICES, cs.ROOT, publish_job=(published.append if published is not None else None)
    )
    return s(world["job"], profile or world["profile"])


def recaps(db):
    with db.connect() as conn:
        return conn.execute(sa.text("SELECT * FROM jobs WHERE kind = 'recap'")).mappings().all()


def matches(db):
    with db.connect() as conn:
        return (
            conn.execute(sa.text("SELECT status, start_s, end_s, job_id FROM matches ORDER BY start_s"))
            .mappings()
            .all()
        )


def test_a_finished_match_becomes_one_recap_render_job_made_of_pieces_of_the_recordings(db, world):
    published: list = []
    assert run(db, world, FakeLLM(), published) == 1
    (job,) = recaps(db)
    assert (job["stage"], job["status"], job["kind"]) == ("render", "queued", "recap") and published == [job["id"]]
    render = job["data"]["render"]
    assert render["hook"] == "Recap seru" and "source_key" not in render
    # moments 150-185, 500-535 (file 0), 750-785 and 950-985 (file 1 starts at 600 s)
    assert [(p["source_key"][-8:], p["start_s"], p["end_s"]) for p in render["pieces"]] == [
        ("00000.ts", 150.0, 185.0), ("00000.ts", 500.0, 535.0), ("00001.ts", 150.0, 185.0), ("00001.ts", 350.0, 385.0),
    ]  # fmt: skip
    assert job["media_key"].endswith("00000.ts")


def test_the_recap_records_which_llm_chose_its_moments(db, world):
    run(db, world, FakeLLM())
    assert recaps(db)[0]["data"]["clip"]["llm"] == "fake/model"
    assert recaps(db)[0]["data"]["clip"]["stt"] == "groq_api/whisper-large-v3-turbo"


def test_the_captions_are_on_the_recaps_own_clock_and_come_from_the_moments_only(db, world):
    run(db, world, FakeLLM())
    words = recaps(db)[0]["data"]["render"]["words"]
    assert words[0] == {"text": "w30", "start_s": 0.0, "end_s": 1.0} and words[-1]["start_s"] >= 105.0
    assert len(words) == 28 and all(w["end_s"] <= 140.01 for w in words)  # 4 moments x 7 words in a 140 s video


def test_the_recap_says_what_it_is_for_review_and_packaging(db, world):
    run(db, world, FakeLLM())
    clip = recaps(db)[0]["data"]["clip"]
    assert clip["kind"] == "recap" and clip["video_id"] == "vid00001" and clip["reason"] == "satu match"
    assert "stream_id" not in clip  # so the recap does not block the ordinary clips of that stretch
    assert clip["score"] == pytest.approx(0.75) and len(clip["moments"]) == 4


def test_the_match_is_remembered_so_the_next_segment_does_not_make_a_second_recap(db, world):
    run(db, world, FakeLLM())
    llm = FakeLLM()
    assert run(db, world, llm) == 0
    assert len(recaps(db)) == 1 and "recap" not in llm.calls  # the match was found again but not asked about again
    assert [m["status"] for m in matches(db)] == ["compiled"]


def test_a_moment_across_the_cut_between_two_files_is_built_from_both(db, world):
    llm = FakeLLM(recap=recap_reply((118, 124, 0.9), (150, 156, 0.8), (190, 196, 0.7)))  # 590-625 s crosses 600 s
    run(db, world, llm)
    pieces = recaps(db)[0]["data"]["render"]["pieces"]
    assert [(p["source_key"][-8:], p["start_s"], p["end_s"]) for p in pieces[:2]] == [
        ("00000.ts", 590.0, 600.0),
        ("00001.ts", 0.0, 25.0),
    ]


def test_a_match_that_is_still_going_makes_nothing(db, world):
    assert run(db, world, FakeLLM(matches=match_reply((20, 199, False)))) == 0
    assert recaps(db) == [] and matches(db) == []


def test_too_few_good_moments_means_no_recap_and_the_match_is_not_asked_about_again(db, world):
    thin = FakeLLM(recap=recap_reply((30, 36, 0.9), (100, 106, 0.8)))
    assert run(db, world, thin) == 0
    assert recaps(db) == [] and [m["status"] for m in matches(db)] == ["skipped"]
    again = FakeLLM()
    run(db, world, again)
    assert "recap" not in again.calls and recaps(db) == []


def test_a_match_whose_recordings_are_gone_is_skipped_with_no_recap(db, world):
    with db.begin() as conn:
        conn.execute(sa.text("UPDATE segments SET deleted_at = clock_timestamp() WHERE idx = 0"))
    assert run(db, world, FakeLLM()) == 0
    assert recaps(db) == [] and [m["status"] for m in matches(db)] == ["skipped"]


def test_both_llm_calls_are_paid_for_in_the_ledger(db, world):
    run(db, world, FakeLLM())
    with db.connect() as conn:
        rows = conn.execute(sa.text("SELECT service, unit, units FROM costs ORDER BY unit, id")).all()
    assert [tuple(r) for r in rows] == [("select", "input_tokens", 500)] * 2 + [("select", "output_tokens", 100)] * 2


def test_a_publish_that_fails_leaves_the_recap_queued_for_the_reconciler(db, world):
    def down(job_id):
        raise ConnectionError("redis is down")

    s = step.RecapStep(db, lambda spec: FakeLLM(), PRICES, cs.ROOT, publish_job=down)
    assert s(world["job"], world["profile"]) == 1 and [j["status"] for j in recaps(db)] == ["queued"]


def test_only_a_profile_with_the_compilation_switched_on_does_anything(db, world):
    off = world["profile"].model_copy(deep=True)
    off.compilation.enabled = False
    llm = FakeLLM()
    assert run(db, world, llm, profile=off) == 0 and llm.calls == []


# ---------- each question can go to its own model, and its own quota gate ----------
def task(model):
    return cs.LLMTask(
        provider="openai_compatible",
        model=model,
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPEN_ROUTER_KEY",
    )


def with_tasks(profile, **tasks):
    profile = profile.model_copy(deep=True)
    profile.selection.llm_tasks = tasks
    return profile


def test_the_two_recap_questions_go_to_their_own_models_and_the_judge_is_the_default(db, world):
    seen: list[str] = []
    llm = FakeLLM()

    def client_for(spec):
        seen.append(spec.model)
        return llm

    profile = with_tasks(world["profile"], recap_match=task("cheap/match"), recap_pick=task("better/pick"))
    step.RecapStep(db, client_for, PRICES, cs.ROOT)(world["job"], profile)
    assert seen == ["cheap/match", "better/pick"]
    seen.clear()
    step.RecapStep(db, client_for, PRICES, cs.ROOT)(
        later(world, 3000.0), with_tasks(world["profile"], recap_pick=task("x/pick"))
    )  # 20 minutes of stream later: the match finder is due again
    assert seen[0] == world["profile"].selection.llm_judge.model  # no recap_match task: the judge asks


class Limited:
    def complete(self, *, system, prompt, max_tokens):
        from common.llm.base import LLMError

        raise LLMError("quota", retryable=True, rate_limited=True, retry_after_s=1800.0)


def test_a_quota_error_pauses_only_the_chain_that_ran_out_and_the_step_waits(db, world):
    from common.llm.gate import Gate, key_of

    alerts: list[str] = []
    ok, limited = FakeLLM(), Limited()
    profile = with_tasks(world["profile"], recap_match=task("cheap/match"), recap_pick=task("better/pick"))
    s = step.RecapStep(
        db, lambda spec: limited if spec.model == "better/pick" else ok, PRICES, cs.ROOT, gate=Gate(db, alerts.append)
    )
    with pytest.raises(jobs.Deferred):
        s(world["job"], profile)
    gate = Gate(db)
    gate.check(profile.selection.llm_for("recap_match"))  # the match finder's chain is fine
    with pytest.raises(jobs.Deferred):
        gate.check(profile.selection.llm_for("recap_pick"))
    assert len(alerts) == 1 and key_of(profile.selection.llm_for("recap_pick")) != key_of(profile.selection.llm_judge)


def test_a_paused_chain_is_not_called_at_all_by_the_step(db, world):
    from common.llm.gate import Gate

    called: list[str] = []

    class Spy(FakeLLM):
        def complete(self, **kw):
            called.append("x")
            return super().complete(**kw)

    profile = with_tasks(world["profile"], recap_match=task("cheap/match"))
    gate = Gate(db, lambda m: None)
    gate.trip(profile.selection.llm_for("recap_match"), __import__("common.llm.base", fromlist=["LLMError"]).LLMError(
        "q", retryable=True, rate_limited=True, retry_after_s=1800.0))  # fmt: skip
    with pytest.raises(jobs.Deferred):
        step.RecapStep(db, lambda spec: Spy(), PRICES, cs.ROOT, gate=gate)(world["job"], profile)
    assert called == []


# ---------- the match finder is not asked after every segment ----------
def later(world, end_s):
    job = world["job"]
    return {**job, "data": {**job["data"], "end_s": end_s, "start_s": end_s - SEG_S}}


def asked(llm):
    return llm.calls.count("match")


def test_the_match_finder_is_asked_on_the_first_segment_then_not_again_until_enough_stream_has_passed(db, world):
    llm = FakeLLM(matches=match_reply())  # nothing finished: so nothing is remembered but the check itself
    s = step.RecapStep(db, lambda spec: llm, PRICES, cs.ROOT)
    profile = world["profile"]
    s(world["job"], profile)
    s(later(world, 1800.0 + 600), profile)  # 10 minutes later: less than check_every_min (20)
    assert asked(llm) == 1
    s(later(world, 1800.0 + 1200), profile)  # 20 minutes after the check
    assert asked(llm) == 2


def test_check_every_min_zero_asks_after_every_segment_as_before(db, world):
    llm = FakeLLM(matches=match_reply())
    profile = world["profile"].model_copy(deep=True)
    profile.compilation.check_every_min = 0
    s = step.RecapStep(db, lambda spec: llm, PRICES, cs.ROOT)
    s(world["job"], profile)
    s(world["job"], profile)
    assert asked(llm) == 2


def test_the_last_segment_of_an_ended_stream_is_asked_but_an_earlier_one_is_throttled(db, world):
    llm = FakeLLM(matches=match_reply())
    s = step.RecapStep(db, lambda spec: llm, PRICES, cs.ROOT)
    with db.begin() as conn:
        conn.execute(sa.text("UPDATE live_streams SET recap_checked_s = 1000 WHERE id = :i"), {"i": STREAM})
        conn.execute(sa.text("UPDATE live_streams SET status = 'ended', ended_at = now() WHERE id = :i"), {"i": STREAM})
    s(later(world, 1200.0), world["profile"])  # 200 s after the check and not the last segment (it ends at 1800)
    assert asked(llm) == 0
    s(world["job"], world["profile"])  # the last segment: nothing comes after it, so it is asked now
    assert asked(llm) == 1
    s(world["job"], world["profile"])
    assert asked(llm) == 1  # and not twice for the same segment


def test_a_check_that_had_to_wait_for_quota_does_not_count_as_a_check(db, world):
    from common.llm.gate import Gate

    s = step.RecapStep(db, lambda spec: Limited(), PRICES, cs.ROOT, gate=Gate(db, lambda m: None))
    with pytest.raises(jobs.Deferred):
        s(world["job"], world["profile"])
    llm = FakeLLM(matches=match_reply())
    step.RecapStep(db, lambda spec: llm, PRICES, cs.ROOT)(world["job"], world["profile"])
    assert asked(llm) == 1


def test_the_default_is_a_check_every_twenty_minutes_of_stream():
    assert cs.Compilation().check_every_min == 20


def fill_the_day(db, n):
    """`n` clips already made in the last 24 hours."""
    with db.begin() as conn:
        for _ in range(n):
            conn.execute(
                sa.text("INSERT INTO jobs (stage, spec, data) VALUES ('review', '{}'::jsonb, '{\"clip\": {}}'::jsonb)")
            )


def test_when_the_days_clips_are_made_no_recap_is_made_and_nothing_is_asked(db, world):
    fill_the_day(db, world["profile"].selection.clips_per_day)
    llm = FakeLLM()
    assert run(db, world, llm) == 0 and llm.calls == [] and recaps(db) == []


def test_a_recap_is_made_while_there_is_room_in_the_day(db, world):
    fill_the_day(db, world["profile"].selection.clips_per_day - 1)
    assert run(db, world, FakeLLM()) == 1


def test_each_moment_of_a_recap_is_cut_to_its_words_when_trim_is_on(db, world):
    run(db, world, FakeLLM())
    plain = [m["end_s"] - m["start_s"] for m in recaps(db)[0]["data"]["clip"]["moments"]]
    with db.begin() as conn:
        conn.execute(sa.text("DELETE FROM jobs WHERE kind = 'recap'"))
        conn.execute(sa.text("DELETE FROM matches"))
        conn.execute(sa.text("UPDATE live_streams SET recap_checked_s = NULL"))
    profile = world["profile"].model_copy(deep=True)
    profile.selection.trim = cs.Trim(lead_s=0.4, tail_s=0.6)
    profile.compilation.length_s = (10, 170)  # the fixtures have one word a sentence, so the cut moments are short
    step.RecapStep(db, lambda spec: FakeLLM(), PRICES, cs.ROOT)(world["job"], profile)
    cut = [m["end_s"] - m["start_s"] for m in recaps(db)[0]["data"]["clip"]["moments"]]
    assert len(cut) == len(plain) and all(8.0 - 1e-6 <= c <= p + 1e-6 for c, p in zip(cut, plain, strict=True))
    assert sum(cut) < sum(plain)  # the silence around the words is gone
