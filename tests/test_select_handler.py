"""The select stage as a worker handler: transcript in, LLM asked, one render job per clip (FR-5, FR-16)."""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import jobs, sources
from common.llm.base import LLMOutputError, LLMResult
from services.select import handler as select

pytestmark = pytest.mark.integration

PRICES = {"fake/model": cs.Price(input=0.1, output=0.4)}
SEG = 100.0  # each test segment is 100 s long: segment 1 covers 100-200 s of the stream
KEY = "segments/s1/00001.ts"


class FakeLLM:
    def __init__(self, candidates=None, text=None):
        self.text = text or json.dumps({"candidates": candidates or []})
        self.calls: list[dict] = []

    def complete(self, *, system, prompt, max_tokens):
        self.calls.append({"system": system, "prompt": prompt, "max_tokens": max_tokens})
        return LLMResult(self.text, 1000, 200, "fake", price_key="fake/model")


def transcript_rows(start, n=20, each=5.0):
    sentences = [
        {"text": f"kalimat {i}", "start_s": start + i * each, "end_s": start + (i + 1) * each} for i in range(n)
    ]
    words = [{"text": f"w{i}", "start_s": start + i * each, "end_s": start + i * each + 1} for i in range(n)]
    return sentences, words


@pytest.fixture
def world(db):
    """A stream with two transcribed segments (0-100 s and 100-200 s) and the select job for the second."""
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        profile = next(p for p in profiles if p.source.id == "leokabuki")
        profile.live.segment_overlap_s = 20  # so 4 sentences of the previous segment are given as context
        profile.selection.trim = None  # cut at the sentence times here: the fixtures have one word to a sentence
        profile.signals.transcript.glossary = []  # no second question to the model: the tests count its calls
        source_id = conn.execute(sa.text("SELECT id FROM sources WHERE source_key = 'leokabuki'")).scalar_one()
        stream = conn.execute(
            sa.text("INSERT INTO live_streams (video_id, source_id) VALUES ('vid00001', :s) RETURNING id"),
            {"s": source_id},
        ).scalar_one()
        for index, start in ((0, 0.0), (1, SEG)):
            sentences, words = transcript_rows(start)
            conn.execute(
                sa.text(
                    "INSERT INTO transcripts "
                    "(stream_id, media_key, start_s, duration_s, language, text, sentences, words) "
                    "VALUES (:st, :k, :a, 100, 'Indonesian', 't', CAST(:se AS jsonb), CAST(:w AS jsonb))"
                ),
                {
                    "st": stream,
                    "k": f"segments/s1/{index:05d}.ts",
                    "a": start,
                    "se": json.dumps(sentences),
                    "w": json.dumps(words),
                },
            )
        job_id = jobs.create_job(
            conn, profile, stage="select", source_id=source_id, media_key=KEY,
            data={
                "live": True, "stream_id": str(stream), "video_id": "vid00001",
                "segment": 1, "start_s": SEG, "end_s": 2 * SEG,
            },
        )  # fmt: skip
        return {"job": jobs.get(conn, job_id), "stream": stream, "source_id": source_id}


def handle(db, world, llm, published=None, **kw):
    handler = select.make_handler(
        db, lambda spec: llm, PRICES, cs.ROOT, publish_job=(published.append if published is not None else None), **kw
    )
    return handler(world["job"])


def render_jobs(db):
    with db.connect() as conn:
        return (
            conn.execute(
                sa.text("SELECT * FROM jobs WHERE stage = 'render' ORDER BY (data->'clip'->>'start_s')::float")
            )
            .mappings()
            .all()
        )


def cand(first, last, **kw):
    return {
        "start_sentence": first,
        "end_sentence": last,
        "kind": "donation",
        "score": 0.9,
        "reason": "donasi dibacakan",
        "hook": "Donasi!",
    } | kw


def test_a_good_moment_becomes_a_queued_render_job_on_the_files_own_clock(db, world):
    published: list = []
    # the prompt numbers 4 context sentences first (80-100 s), so segment sentence 0 is number 4: 6..12 = 110-145 s
    assert handle(db, world, FakeLLM([cand(6, 12)]), published) is None  # the segment job itself is finished
    (job,) = render_jobs(db)
    assert job["status"] == "queued" and job["kind"] == "donation" and job["media_key"] == KEY
    assert job["source_id"] == world["source_id"] and published == [job["id"]]
    clip, render = job["data"]["clip"], job["data"]["render"]
    assert (clip["start_s"], clip["end_s"]) == (110.0, 145.0)  # sentences 2 and 8 of the segment: 5 s each
    assert clip["stream_id"] == str(world["stream"]) and clip["segment_job"] == str(world["job"]["id"])
    assert clip["video_id"] == "vid00001"  # so package can link to the full stream
    assert clip["score"] == 0.9 and clip["reason"] == "donasi dibacakan"
    assert (render["source_key"], render["start_s"], render["end_s"]) == (
        KEY,
        10.0,
        45.0,
    )  # stream 110 - segment start 100
    assert render["hook"] == "Donasi!"
    assert (
        render["words"][0] == {"text": "w2", "start_s": 10.0, "end_s": 11.0} and len(render["words"]) == 7
    )  # file clock


def test_the_clip_records_which_llm_chose_it_and_which_engine_transcribed_it(db, world):
    handle(db, world, FakeLLM([cand(6, 12)]))
    (job,) = render_jobs(db)
    assert job["data"]["clip"]["llm"] == "fake/model"  # the model that actually answered, whatever was configured
    assert job["data"]["clip"]["stt"] == "groq_api/whisper-large-v3-turbo"


def test_the_previous_segments_tail_is_given_as_context_only(db, world):
    llm = FakeLLM()
    handle(db, world, llm)
    prompt = llm.calls[0]["prompt"]
    context = [line for line in prompt.splitlines() if "] (context)" in line]
    assert [line.split()[-2:] for line in context] == [["kalimat", str(n)] for n in (16, 17, 18, 19)]  # the last 20 s


def test_a_candidate_that_starts_in_the_context_is_dropped(db, world):
    handle(db, world, FakeLLM([cand(1, 8)]))
    assert render_jobs(db) == []


def test_the_llm_is_paid_for_in_the_ledger_even_when_nothing_is_picked(db, world):
    handle(db, world, FakeLLM([]))
    with db.connect() as conn:
        rows = conn.execute(sa.text("SELECT service, provider, unit, units FROM costs ORDER BY unit")).all()
    assert [tuple(r) for r in rows] == [
        ("select", "fake", "input_tokens", 1000),
        ("select", "fake", "output_tokens", 200),
    ]


def test_running_the_job_twice_makes_no_second_set_of_clips_and_asks_the_llm_once(db, world):
    llm = FakeLLM([cand(6, 12)])
    handle(db, world, llm)
    handle(db, world, llm)
    assert len(render_jobs(db)) == 1 and len(llm.calls) == 1


def test_a_clip_overlapping_one_already_made_from_the_stream_is_dropped(db, world):
    handle(db, world, FakeLLM([cand(6, 12)]))
    with db.begin() as conn:  # another select job for the same segment and stream
        profile = cs.Profile.model_validate(world["job"]["spec"])
        later = jobs.create_job(
            conn, profile, stage="select", source_id=world["source_id"], media_key=KEY, data=world["job"]["data"]
        )
        other = {**world, "job": jobs.get(conn, later)}
    handle(db, other, FakeLLM([cand(7, 13)]))
    assert len(render_jobs(db)) == 1


def test_a_queue_that_is_down_leaves_the_jobs_queued_for_the_reconciler(db, world):
    def down(job_id):
        raise ConnectionError("redis is down")

    handler = select.make_handler(db, lambda spec: FakeLLM([cand(6, 12)]), PRICES, cs.ROOT, publish_job=down)
    assert handler(world["job"]) is None
    assert [j["status"] for j in render_jobs(db)] == ["queued"]


def test_a_reply_that_is_not_the_agreed_shape_is_an_error_for_the_worker(db, world):
    with pytest.raises(LLMOutputError):
        handle(db, world, FakeLLM(text="I think the best moment is at 2:10"))
    assert render_jobs(db) == []


def test_a_profile_that_selects_from_signals_only_does_nothing_here(db, world):
    spec = {**world["job"]["spec"]}
    spec["selection"] = {**spec["selection"], "mode": "signals"}
    spec["signals"] = {**spec["signals"], "transcript": {**spec["signals"]["transcript"], "mode": "candidates_only"}}
    spec["compilation"] = {**spec["compilation"], "enabled": False}  # a recap needs the full transcript too
    llm = FakeLLM([cand(6, 12)])
    assert handle(db, {**world, "job": {**world["job"], "spec": spec}}, llm) is None
    assert llm.calls == [] and render_jobs(db) == []


def test_a_silent_segment_is_finished_without_asking_the_llm(db, world):
    with db.begin() as conn:
        conn.execute(
            sa.text("UPDATE transcripts SET sentences = '[]'::jsonb, words = '[]'::jsonb WHERE media_key = :k"),
            {"k": KEY},
        )
    llm = FakeLLM([cand(0, 5)])
    assert handle(db, world, llm) is None and llm.calls == []


def test_a_segment_with_no_transcript_yet_is_an_error_so_the_job_is_retried(db, world):
    with db.begin() as conn:
        conn.execute(sa.text("DELETE FROM transcripts WHERE media_key = :k"), {"k": KEY})
    with pytest.raises(select.SelectError, match="no transcript"):
        handle(db, world, FakeLLM())


# ---------- the recap step runs after the ordinary clips, and never breaks them ----------------------------
class RecapSpy:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def __call__(self, job, profile):
        self.calls.append(job["id"])
        if self.error:
            raise self.error
        return 0


def with_recap(db, world, llm, recap, alerts=None):
    return select.make_handler(
        db, lambda spec: llm, PRICES, cs.ROOT, recap=recap, alert=(alerts.append if alerts is not None else None)
    )(world["job"])


def test_the_recap_step_runs_after_the_clips_when_the_profile_has_compilation_on(db, world):
    spy = RecapSpy()
    with_recap(db, world, FakeLLM([cand(6, 12)]), spy)
    assert spy.calls == [world["job"]["id"]] and len(render_jobs(db)) == 1


def test_the_recap_step_is_not_called_when_compilation_is_off(db, world):
    spec = {**world["job"]["spec"], "compilation": {**world["job"]["spec"]["compilation"], "enabled": False}}
    spy = RecapSpy()
    select.make_handler(db, lambda spec_: FakeLLM(), PRICES, cs.ROOT, recap=spy)({**world["job"], "spec": spec})
    assert spy.calls == []


def test_a_recap_step_that_fails_does_not_lose_the_clips_and_raises_an_alert(db, world):
    alerts: list[str] = []
    assert with_recap(db, world, FakeLLM([cand(6, 12)]), RecapSpy(RuntimeError("model down")), alerts) is None
    assert len(render_jobs(db)) == 1 and any("model down" in a for a in alerts)


# ---------- when every LLM is out of quota --------------------------------------------------------
class OutOfQuota:
    def __init__(self):
        self.calls = 0

    def complete(self, *, system, prompt, max_tokens):
        from common.llm.base import LLMError

        self.calls += 1
        raise LLMError("429 quota", retryable=True, rate_limited=True, retry_after_s=1800.0)


def gated(db, world, llm, alerts):
    from common.llm.gate import Gate

    return select.make_handler(db, lambda spec: llm, PRICES, cs.ROOT, gate=Gate(db, alerts.append))(world["job"])


def test_a_quota_error_makes_the_job_wait_instead_of_fail_and_alerts_once(db, world):
    alerts: list[str] = []
    llm = OutOfQuota()
    with pytest.raises(jobs.Deferred) as first:
        gated(db, world, llm, alerts)
    assert 290 <= first.value.wait_s <= 1800 and len(alerts) == 1 and llm.calls == 1


def test_while_the_quota_is_used_up_the_next_job_does_not_even_ask_and_does_not_alert(db, world):
    alerts: list[str] = []
    llm = OutOfQuota()
    with pytest.raises(jobs.Deferred):
        gated(db, world, llm, alerts)
    with pytest.raises(jobs.Deferred):
        gated(db, world, llm, alerts)
    assert llm.calls == 1 and len(alerts) == 1  # the second job waited without calling anyone


def test_an_error_that_is_not_a_quota_problem_still_fails_the_job(db, world):
    from common.llm.base import LLMError

    class Broken:
        def complete(self, *, system, prompt, max_tokens):
            raise LLMError("bad key", retryable=False)

    alerts: list[str] = []
    with pytest.raises(LLMError, match="bad key"):
        gated(db, world, Broken(), alerts)
    assert alerts == []


def shout(db, text="Maniac!", at=10):
    with db.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE transcripts SET sentences = jsonb_set(sentences, CAST(:p AS text[]), CAST(:t AS jsonb)) "
                "WHERE media_key = :k"
            ),
            {"p": f"{{{at},text}}", "t": json.dumps(text), "k": KEY},
        )


def with_callouts(world):
    """The MLBB profile has no call-outs (its announcer speaks for either team): a profile that does is made here."""
    spec = {**world["job"]["spec"]}
    spec["selection"] = {
        **spec["selection"],
        "callouts": {"terms": ["maniac", "savage"], "ignore_near": ["pesan"], "before_s": 20, "after_s": 10},
    }
    return {**world, "job": {**world["job"], "spec": spec}}


def test_without_call_outs_a_shouted_kill_word_alone_makes_no_clip(db, world):
    shout(db)
    handle(db, world, FakeLLM([]))
    assert render_jobs(db) == []


def test_a_kill_call_out_makes_a_highlight_even_when_the_judge_proposed_nothing(db, world):
    shout(db)
    llm = FakeLLM([])
    handle(db, with_callouts(world), llm)
    (job,) = render_jobs(db)
    clip = job["data"]["clip"]
    assert job["kind"] == "highlight" and (clip["start_s"], clip["end_s"]) == (130.0, 165.0)
    assert "maniac" in clip["reason"] and job["data"]["render"]["hook"] == "MANIAC!"


def test_a_call_out_in_a_viewers_message_makes_no_clip(db, world):
    shout(db, "Tiga dari Samuan pesan dapat Savage")
    handle(db, with_callouts(world), FakeLLM([]))
    assert render_jobs(db) == []


# ---------- chat spikes ----------
def chat_burst(db, *, at_s=140.0, quiet=False):
    """A segment row for the world's segment 1 (stream clock 100-200 s), a trickle of chat and a burst at `at_s`."""
    with db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO segments (stream_id, idx, key, start_s, end_s) VALUES (:s, 1, :k, 100, 200) "
                "ON CONFLICT DO NOTHING"
            ),
            {"s": world_stream(conn), "k": KEY},
        )
        stream = world_stream(conn)
        wall_end = float(conn.execute(sa.text("SELECT extract(epoch FROM created_at) FROM segments")).scalar_one())
        wall_start = wall_end - 100
        for i in range(10):
            conn.execute(
                sa.text("INSERT INTO chat_counts (stream_id, bucket_at, n) VALUES (:s, to_timestamp(:t), 1)"),
                {"s": stream, "t": wall_start + 10 * i + 12},
            )
        if not quiet:
            for k, n in ((0, 8), (10, 6)):
                conn.execute(
                    sa.text(
                        "INSERT INTO chat_counts (stream_id, bucket_at, n) VALUES (:s, to_timestamp(:t), :n) "
                        "ON CONFLICT (tenant_id, stream_id, bucket_at) DO UPDATE SET n = EXCLUDED.n"
                    ),
                    {"s": stream, "t": wall_start + (at_s - 100) + k + 12, "n": n},
                )


def world_stream(conn):
    return conn.execute(sa.text("SELECT id FROM live_streams WHERE video_id = 'vid00001'")).scalar_one()


def test_a_chat_spike_is_marked_in_what_the_judge_reads_and_its_moment_can_become_a_clip(db, world):
    chat_burst(db)
    llm = FakeLLM([cand(12, 17, kind="highlight")])  # segment sentences 8-13 (140-170 s) are numbers 12-17
    handle(db, world, llm)
    assert "[chat spike:" in llm.calls[0]["prompt"]
    (job,) = render_jobs(db)
    assert job["kind"] == "highlight" and job["data"]["clip"]["start_s"] == 140.0


def test_a_segment_without_a_chat_burst_is_read_as_before(db, world):
    chat_burst(db, quiet=True)
    llm = FakeLLM([])
    handle(db, world, llm)
    assert "[chat spike" not in llm.calls[0]["prompt"]


def test_a_silent_segment_with_a_chat_spike_is_still_looked_at(db, world):
    with db.begin() as conn:
        conn.execute(
            sa.text("UPDATE transcripts SET sentences = '[]'::jsonb, words = '[]'::jsonb WHERE media_key = :k"),
            {"k": KEY},
        )
    chat_burst(db)
    llm = FakeLLM([cand(4, 4, kind="highlight")])
    handle(db, world, llm)
    assert "nobody spoke" in llm.calls[0]["prompt"]


# ---------- the day's clips ----------
def fill_the_day(db, n, *, review=None):
    data: dict = {"clip": {}} | ({"review": {"state": review}} if review else {})
    with db.begin() as conn:
        for _ in range(n):
            conn.execute(
                sa.text("INSERT INTO jobs (stage, spec, data) VALUES ('review', '{}'::jsonb, CAST(:d AS jsonb))"),
                {"d": json.dumps(data)},
            )


def limit_of(world):
    return cs.Profile.model_validate(world["job"]["spec"]).selection.clips_per_day


def test_when_the_days_clips_are_made_the_segment_is_finished_without_asking_the_llm(db, world):
    fill_the_day(db, limit_of(world))
    llm = FakeLLM([cand(6, 12)])
    assert handle(db, world, llm) is None and llm.calls == [] and render_jobs(db) == []


def test_a_rejected_clip_gives_its_place_back(db, world):
    fill_the_day(db, limit_of(world), review="rejected")
    llm = FakeLLM([cand(6, 12)])
    handle(db, world, llm)
    assert len(llm.calls) == 1 and len(render_jobs(db)) == 1


def test_only_as_many_clips_are_made_as_there_is_room_for(db, world):
    fill_the_day(db, limit_of(world) - 1)
    llm = FakeLLM([cand(6, 12, score=0.9), cand(14, 19, score=0.8)])  # two good moments, room for one
    handle(db, world, llm)
    clips = [j for j in render_jobs(db) if j["stage"] == "render"]
    assert len(clips) == 1 and clips[0]["data"]["clip"]["score"] == 0.9  # the better one


def test_a_limit_set_from_telegram_replaces_the_profiles_and_zero_means_no_limit(db, world):
    from common import rules

    fill_the_day(db, limit_of(world))
    with db.begin() as conn:
        rules.set_value(conn, "clips", 0, rules.Ceilings(20, 720, 40), "telegram:1")
    llm = FakeLLM([cand(6, 12)])
    handle(db, world, llm)
    assert len(llm.calls) == 1 and len(render_jobs(db)) == 1


# ---------- cut to the words ----------
def speak(db, spans):
    """Replace the words of the world's segment with `spans` (stream clock)."""
    words = [{"text": f"w{i}", "start_s": a, "end_s": b} for i, (a, b) in enumerate(spans)]
    with db.begin() as conn:
        conn.execute(
            sa.text("UPDATE transcripts SET words = CAST(:w AS jsonb) WHERE media_key = :k"),
            {"w": json.dumps(words), "k": KEY},
        )


def trimmed(world, **rules):
    spec = {**world["job"]["spec"]}
    spec["selection"] = {**spec["selection"], "trim": rules or {"lead_s": 0.4, "tail_s": 0.6}}
    return {**world, "job": {**world["job"], "spec": spec}}


def test_a_clip_is_cut_to_its_words_not_to_the_sentence_times(db, world):
    speak(db, [(112.0, 112.4), (120.0, 125.0), (140.0, 144.5), (160.0, 161.0)])  # the sentences start at 110
    handle(db, trimmed(world), FakeLLM([cand(6, 12)]))  # sentences 110-145
    (job,) = render_jobs(db)
    clip, render = job["data"]["clip"], job["data"]["render"]
    assert (clip["start_s"], clip["end_s"]) == (pytest.approx(111.6), pytest.approx(145.1))
    assert (render["start_s"], render["end_s"]) == (pytest.approx(11.6), pytest.approx(45.1))  # on the file's clock
    assert [w["text"] for w in render["words"]] == ["w0", "w1", "w2"]  # the captions are the words in the cut


def test_with_trim_off_the_clip_keeps_the_sentence_times(db, world):
    speak(db, [(112.0, 112.4), (140.0, 144.5)])
    handle(db, world, FakeLLM([cand(6, 12)]))  # the fixtures switch trim off
    (job,) = render_jobs(db)
    assert (job["data"]["clip"]["start_s"], job["data"]["clip"]["end_s"]) == (110.0, 145.0)


def test_a_cut_that_would_leave_too_short_a_clip_is_kept_as_long_as_the_kind_needs(db, world):
    speak(db, [(130.0, 131.0), (132.0, 133.0)])  # all the speech is in 3 s of a 35 s clip
    handle(db, trimmed(world), FakeLLM([cand(6, 12)]))
    (job,) = render_jobs(db)
    clip = job["data"]["clip"]
    assert clip["end_s"] - clip["start_s"] >= 20.0 - 1e-6  # the shortest a donation clip may be


# ---------- game names Whisper misheard ----------
class Routing(FakeLLM):
    """Answers the judge's question with candidates and the glossary question with replacements."""

    def __init__(self, candidates, replacements):
        super().__init__(candidates)
        self.replacements, self.kinds = replacements, []

    def complete(self, *, system, prompt, max_tokens):
        if "Glossary:" in prompt:
            self.kinds.append("fix")
            return LLMResult(json.dumps({"replacements": self.replacements}), 300, 40, "fake", price_key="fake/model")
        self.kinds.append("judge")
        return super().complete(system=system, prompt=prompt, max_tokens=max_tokens)


def with_glossary(world, glossary=("Ruby", "Nolan")):
    spec = {**world["job"]["spec"]}
    spec["signals"] = {**spec["signals"], "transcript": {**spec["signals"]["transcript"], "glossary": list(glossary)}}
    return {**world, "job": {**world["job"], "spec": spec}}


def say(db, texts, start=112.0):
    words = [{"text": t, "start_s": start + i * 2.0, "end_s": start + i * 2.0 + 1.0} for i, t in enumerate(texts)]
    with db.begin() as conn:
        conn.execute(
            sa.text("UPDATE transcripts SET words = CAST(:w AS jsonb) WHERE media_key = :k"),
            {"w": json.dumps(words), "k": KEY},
        )


def test_a_misheard_hero_name_in_a_clips_words_is_fixed_before_the_clip_is_rendered(db, world):
    say(db, ["kita", "berharap", "Robby", "dapat", "momen", "dua", "orang"])
    llm = Routing([cand(6, 12)], [{"from": "Robby", "to": "Ruby"}])
    handle(db, with_glossary(world), llm)
    (job,) = render_jobs(db)
    assert [w["text"] for w in job["data"]["render"]["words"]][:3] == ["kita", "berharap", "Ruby"]
    assert llm.kinds == ["judge", "fix"]


def test_both_questions_are_paid_for_in_the_ledger(db, world):
    say(db, ["kita", "berharap", "Robby", "dapat", "momen"])
    handle(db, with_glossary(world), Routing([cand(6, 12)], [{"from": "Robby", "to": "Ruby"}]))
    with db.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM costs WHERE service = 'select'")).scalar_one() >= 2


def test_a_glossary_question_the_model_cannot_answer_does_not_stop_the_clip(db, world):
    say(db, ["kita", "berharap", "Robby", "dapat", "momen"])

    class Down(Routing):
        def complete(self, *, system, prompt, max_tokens):
            if "Glossary:" in prompt:
                raise LLMOutputError("not json")
            return super().complete(system=system, prompt=prompt, max_tokens=max_tokens)

    handle(db, with_glossary(world), Down([cand(6, 12)], []))
    (job,) = render_jobs(db)
    assert "Robby" in [w["text"] for w in job["data"]["render"]["words"]]  # the words as Whisper wrote them


def test_a_replacement_to_a_word_that_is_not_in_the_glossary_is_not_made(db, world):
    say(db, ["kita", "berharap", "Robby", "dapat", "momen"])
    handle(db, with_glossary(world), Routing([cand(6, 12)], [{"from": "Robby", "to": "Zilong"}]))
    (job,) = render_jobs(db)
    assert "Robby" in [w["text"] for w in job["data"]["render"]["words"]]


def test_a_clip_is_not_asked_about_when_the_profile_has_no_glossary(db, world):
    say(db, ["kita", "berharap", "Robby", "dapat", "momen"])
    llm = Routing([cand(6, 12)], [])
    handle(db, world, llm)
    assert llm.kinds == ["judge"]
