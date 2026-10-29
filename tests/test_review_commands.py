"""Commands in the Telegram chat: /status, /posts, /dead and /help. Read-only except the Requeue button on a dead
job. Only the allowed user ids, in a private chat, with an exact command word: nothing else you type is read."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import yaml
from fake_telegram_server import fake_telegram

import config_schema as cs
from common import jobs, ledger, pauses, sources
from common.telegram import Telegram
from services.publisher import accounts as acc
from services.publisher import posts
from services.review import commands as cmd
from services.review.listener import handle_update
from services.understand import budget

pytestmark = pytest.mark.integration

ME, STRANGER = 111, 222
TOKEN = "123456789:FAKE-token_for_tests"  # noqa: S105  # gitleaks:allow
LIMITS = budget.Limits(audio_seconds_per_hour=7200, audio_seconds_per_day=28800, alert_at=0.8, stop_at=0.9)
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)  # 19:00 in Jakarta: inside the 18:00-22:00 window


def accounts(tmp_path: Path, windows: list[str]) -> acc.Accounts:
    rows = [
        {"id": "yt_test", "platform": "shorts", "test": True, "daily_cap": 6},
        {"id": "tt_test", "platform": "tiktok", "test": True, "daily_cap": 3},
    ]
    defaults = {"daily_cap": 3, "min_spacing_min": 120, "post_windows": windows, "visibility": "public"}
    path = tmp_path / "accounts.yaml"
    path.write_text(yaml.safe_dump({"timezone": "Asia/Jakarta", "defaults": defaults, "accounts": rows}))
    return acc.load(path)


@pytest.fixture
def world(db, tmp_path):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        profile = next(p for p in profiles if p.source.id == "leokabuki")
        profile.test_targets = [cs.Target(account="yt_test"), cs.Target(account="tt_test")]
    ctx = cmd.Commands(db, LIMITS, accounts(tmp_path, ["18:00-22:00"]), now=lambda: NOW)
    return ctx, profile, tmp_path


def make_job(db, profile, stage, status="queued", **kw):
    with db.begin() as conn:
        job_id = jobs.create_job(conn, profile, stage=stage, **kw)
        if status != "queued":
            conn.execute(sa.text("UPDATE jobs SET status = :s WHERE id = :i"), {"s": status, "i": job_id})
        return job_id


# ---------- recognising a command ----------
@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("/status", "status"),
        ("/status@MyFactoryBot", "status"),
        ("  /dead ", "dead"),
        ("/posts", "posts"),
        ("/help", "help"),
    ],
)
def test_an_exact_command_word_is_recognised_with_or_without_the_bots_name(text, name):
    assert cmd.parse(text) == name


@pytest.mark.parametrize(
    "text", ["status", "/status now", "/Status", "/status;drop", "hello", "", None, 5, "/" + "a" * 50, "/status\n/dead"]
)
def test_anything_else_is_not_a_command(text):
    assert cmd.parse(text) is None


# ---------- /status ----------
def test_status_counts_the_jobs_of_each_stage_by_what_they_are_doing(db, world):
    ctx, profile, _ = world
    for _ in range(2):
        make_job(db, profile, "select")
    make_job(db, profile, "select", "dead")
    with db.begin() as conn:
        for _ in range(3):
            jobs.defer(conn, jobs.create_job(conn, profile, stage="select"), 3600, "out of quota")
    text = ctx.status()
    line = next(x for x in text.splitlines() if x.strip().startswith("select"))
    assert "2 queued" in line and "3 waiting to retry" in line and "1 dead" in line


def test_status_says_how_many_clips_wait_for_your_decision(db, world):
    ctx, profile, _ = world
    for _ in range(2):
        make_job(db, profile, "review", "waiting")
    assert "Waiting for you: 2" in ctx.status()


def test_status_with_nothing_going_on_says_so(db, world):
    assert "Nothing is queued" in world[0].status()


def test_status_shows_groq_use_and_whether_transcription_is_stopped(db, world):
    ctx, _, _ = world
    with db.begin() as conn:
        ledger.record_cost(
            conn,
            profile="mlbb",
            service="understand",
            provider="groq",
            units=Decimal(26500),
            unit="audio_s",
            usd=Decimal(0),
        )
    text = ctx.status()
    assert "92%" in text and "STOPPED" in text and "90%" in text


def test_status_says_groq_is_running_while_there_is_room(db, world):
    ctx, _, _ = world
    with db.begin() as conn:
        ledger.record_cost(
            conn,
            profile="mlbb",
            service="understand",
            provider="groq",
            units=Decimal(5000),
            unit="audio_s",
            usd=Decimal(0),
        )
    text = ctx.status()
    assert "17%" in text and "running" in text and "STOPPED" not in text


def test_status_shows_an_llm_pause_and_otherwise_says_the_llm_is_fine(db, world):
    ctx, _, _ = world
    assert "LLM: ok" in ctx.status()
    with db.begin() as conn:
        pauses.start(conn, "llm:openai_compatible/gemini-3.5-flash", 1800, "every LLM provider is out of quota")
    text = ctx.status()
    assert "LLM: paused" in text and "out of quota" in text


def test_status_lists_what_is_being_recorded_right_now(db, world):
    ctx, _, _ = world
    with db.begin() as conn:
        source = conn.execute(sa.text("SELECT id FROM sources WHERE source_key = 'leokabuki'")).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO live_streams (source_id, video_id, status, lease_worker, lease_heartbeat_at) "
                "VALUES (:s, 'abcdefghijk', 'live', 'recorder-1', clock_timestamp())"
            ),
            {"s": source},
        )
    assert "Recording now: leokabuki" in ctx.status()


def test_a_live_stream_nobody_holds_is_not_called_recorded(db, world):
    ctx, _, _ = world
    with db.begin() as conn:
        source = conn.execute(sa.text("SELECT id FROM sources WHERE source_key = 'leokabuki'")).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO live_streams (source_id, video_id, status, lease_worker, lease_heartbeat_at) "
                "VALUES (:s, 'abcdefghijk', 'live', 'recorder-1', clock_timestamp() - interval '10 minutes')"
            ),
            {"s": source},
        )
        conn.execute(
            sa.text("INSERT INTO live_streams (source_id, video_id, status) VALUES (:s, 'bbbbbbbbbbb', 'live')"),
            {"s": source},
        )
    text = ctx.status()
    assert "Recording now: nobody" in text and "Live but not being recorded: leokabuki (2 streams)" in text


def test_status_says_which_accounts_have_clips_waiting_and_why(db, world):
    ctx, profile, tmp = world
    ctx_outside = cmd.Commands(db, LIMITS, accounts(tmp, ["01:00-02:00"]), now=lambda: NOW)  # 19:00 is outside it
    job = make_job(db, profile, "publish")
    with db.begin() as conn:
        post, _ = posts.claim(conn, job, "yt_test", "shorts")
        posts.mark_posted(conn, post["id"], "v1", "https://youtu.be/v1")
    text = ctx_outside.status()
    assert (
        "tt_test: 1 waiting (outside the posting windows)" in text and "yt_test" not in text.split("Waiting to post")[1]
    )


def test_a_clip_that_may_go_now_is_waiting_only_for_the_next_sweep(db, world):
    ctx, profile, _ = world
    make_job(db, profile, "publish")
    assert "tt_test: 1 waiting (ready: goes on the next sweep)" in ctx.status()


def test_status_has_a_header_and_blank_lines_between_its_groups(db, world):
    ctx, profile, _ = world
    make_job(db, profile, "select")
    make_job(db, profile, "publish")
    groups = ctx.status().split("\n\n")
    assert groups[0].startswith("📊 Status") and groups[1].startswith("🔧 Jobs:") and groups[2].startswith("🎙 Groq")
    assert any(g.startswith("📤 Waiting to post:") for g in groups)


def test_status_opens_with_a_one_line_summary_of_what_needs_attention(db, world):
    ctx, profile, _ = world
    assert ctx.status().splitlines()[1] == "✅ Nothing needs attention"
    make_job(db, profile, "render", "dead")
    make_job(db, profile, "select", "dead")
    ctx.expected, ctx.stale_after_s = ["a", "b"], 120
    with db.begin() as conn:
        pauses.start(conn, "llm:openai_compatible/gemini-3.5-flash", 1800, "out of quota")
    summary = ctx.status().splitlines()[1]
    assert summary == "⚠️ Needs attention: 2 dead jobs · 2 services down · LLM paused"


def test_a_stopped_groq_is_part_of_the_summary(db, world):
    ctx, _, _ = world
    with db.begin() as conn:
        ledger.record_cost(
            conn, profile="mlbb", service="understand", provider="groq", units=Decimal(26500), unit="audio_s",
            usd=Decimal(0),
        )  # fmt: skip
    assert "Groq stopped" in ctx.status().splitlines()[1]


def test_the_chat_version_bolds_each_key_and_escapes_everything_else():
    text = "📊 Status\n⚠️ Needs attention: 1 dead job\n\n🔧 Jobs:\n  select: 2 queued\nPaused: source <b>x</b> & y"
    assert cmd.to_html(text) == (
        "<b>📊 Status</b>\n⚠️ <b>Needs attention:</b> 1 dead job\n\n🔧 <b>Jobs:</b>\n  <b>select:</b> 2 queued\n"
        "<b>Paused:</b> source &lt;b&gt;x&lt;/b&gt; &amp; y"
    )


def test_status_names_the_services_that_are_down_only_when_there_are_a_few(db, world):
    ctx, _, _ = world
    names = ["a", "b", "c", "d", "e"]
    ctx.expected, ctx.stale_after_s = names, 120
    assert "Services: 0 of 5 up (5 not up, see /health)" in ctx.status()
    ctx.expected = names[:2]
    assert "Services: 0 of 2 up (not up: a, b)" in ctx.status()


# ---------- /posts ----------
def test_posts_shows_the_last_24_hours_against_the_cap_for_each_account(db, world):
    ctx, profile, _ = world
    ctx.now = lambda: datetime.now(UTC)  # the posts are stamped with the database's clock: the real one
    job = make_job(db, profile, "publish")
    with db.begin() as conn:
        post, _ = posts.claim(conn, job, "tt_test", "tiktok")
        posts.mark_posted(conn, post["id"], "telegram:1", "")
        old = jobs.create_job(conn, profile, stage="publish")
        older, _ = posts.claim(conn, old, "tt_test", "tiktok")
        conn.execute(
            sa.text("UPDATE posts SET created_at = clock_timestamp() - interval '25 hours' WHERE id = :i"),
            {"i": older["id"]},
        )
    text = ctx.posts()
    assert "tt_test 1/3" in text and "yt_test 0/6" in text and "last" in text and "24 hours" in text  # 25 h old: out


def test_posts_uses_the_cap_you_set_from_telegram(db, world):
    from common import rules

    ctx, _, _ = world
    ctx.now = lambda: datetime.now(UTC)
    with db.begin() as conn:
        rules.set_value(conn, "cap", 5, rules.Ceilings(20, 720, 40), "telegram:1")
    assert "yt_test 0/5" in ctx.posts() and "tt_test 0/5" in ctx.posts()


# ---------- /dead ----------
def test_dead_jobs_are_listed_newest_first_with_their_error_and_a_total(db, world):
    ctx, profile, _ = world
    with db.begin() as conn:
        for n in range(7):
            job_id = jobs.create_job(conn, profile, stage="select")
            conn.execute(
                sa.text(
                    "UPDATE jobs SET status = 'dead', last_error = :e, "
                    "updated_at = clock_timestamp() + make_interval(secs => :n) WHERE id = :i"
                ),
                {"e": f"LLMError number {n} " + "x" * 400, "n": n, "i": job_id},
            )
    listed, total = ctx.dead(limit=5)
    assert total == 7 and len(listed) == 5 and "number 6" in listed[0].error and len(listed[0].error) <= 200
    assert listed[0].stage == "select"


def test_no_dead_jobs_gives_an_empty_list(db, world):
    assert world[0].dead() == ([], 0)


# ---------- the chat ----------
def message(text, user=ME, chat_type="private", chat_id=None):
    return {
        "update_id": 1,
        "message": {"from": {"id": user}, "chat": {"id": chat_id or user, "type": chat_type}, "text": text},
    }


@pytest.fixture
def chat(db, world):
    ctx, profile, _ = world
    requeued: list[tuple[uuid.UUID, str]] = []
    with fake_telegram() as (fake, url):
        bot = Telegram(TOKEN, base_url=url)

        def send(update, **kw: Any):
            handle_update(
                update, engine=db, bot=bot, allowed={ME}, publish_job=None, commands=ctx,
                requeue_job=lambda job_id, stage: requeued.append((job_id, stage)), **kw,
            )  # fmt: skip

        yield send, fake, requeued, profile


def test_a_command_from_you_in_a_private_chat_is_answered_with_plain_text(chat):
    send, fake, _, _ = chat
    send(message("/posts"))
    (reply,) = fake.calls_to("sendMessage")
    assert reply.fields["chat_id"] == ME and "parse_mode" not in reply.fields


def test_status_is_sent_as_html_with_bold_keys_and_nothing_untrusted_left_unescaped(chat, db):
    send, fake, _, _ = chat
    with db.begin() as conn:
        pauses.start(conn, "llm:x", 1800, "<script>alert(1)</script> out of quota")
    send(message("/status"))
    (reply,) = fake.calls_to("sendMessage")
    text = reply.fields["text"]
    assert reply.fields["parse_mode"] == "HTML" and "<b>Groq audio:</b>" in text
    assert "<script>" not in text and "&lt;script&gt;" in text


def test_help_lists_the_commands(chat):
    send, fake, _, _ = chat
    send(message("/help"))
    text = fake.calls_to("sendMessage")[0].fields["text"]
    assert all(word in text for word in ("/status", "/posts", "/dead"))


def test_an_unknown_command_gets_the_help_and_plain_text_gets_nothing(chat):
    send, fake, _, _ = chat
    send(message("/deleteeverything"))
    assert "/status" in fake.calls_to("sendMessage")[0].fields["text"]
    fake.calls.clear()
    send(message("how many jobs are there?"))
    assert fake.calls == []


def test_a_stranger_a_group_or_a_forwarded_chat_is_ignored_without_an_answer(chat):
    send, fake, _, _ = chat
    send(message("/status", user=STRANGER))
    send(message("/status", chat_type="group", chat_id=-100))
    send(message("/status", chat_id=999))  # a private chat that is not with the sender
    assert fake.calls == []


def test_dead_jobs_come_one_message_each_with_a_requeue_button(chat, db):
    send, fake, _, profile = chat
    ids = [make_job(db, profile, "select", "dead") for _ in range(2)]
    send(message("/dead"))
    messages = fake.calls_to("sendMessage")
    assert len(messages) == 3  # a header and one per job
    buttons = [m.fields["reply_markup"]["inline_keyboard"][0][0]["callback_data"] for m in messages[1:]]
    assert sorted(buttons) == sorted(f"q:{i}" for i in ids)


def test_dead_with_none_says_so(chat):
    send, fake, _, _ = chat
    send(message("/dead"))
    assert "No dead jobs" in fake.calls_to("sendMessage")[0].fields["text"]


def tap(data, user=ME):
    return {"update_id": 2, "callback_query": {"id": "cb", "from": {"id": user}, "data": data,
                                                "message": {"message_id": 77, "chat": {"id": ME}}}}  # fmt: skip


def test_pressing_requeue_gives_the_job_a_fresh_start_and_queues_it_again(chat, db):
    send, fake, requeued, profile = chat
    job_id = make_job(db, profile, "select", "dead")
    send(tap(f"q:{job_id}"))
    with db.connect() as conn:
        job = jobs.get(conn, job_id)
    assert job is not None and (job["status"], job["attempt"]) == ("queued", 0)
    assert requeued == [(job_id, "select")]
    assert "Requeued" in fake.calls_to("answerCallbackQuery")[0].fields["text"]
    edit = fake.calls_to("editMessageText")[0].fields
    assert (edit["chat_id"], edit["message_id"]) == (ME, 77) and "requeued" in edit["text"].lower()


def test_pressing_requeue_for_a_job_that_is_not_dead_changes_nothing(chat, db):
    send, fake, requeued, profile = chat
    job_id = make_job(db, profile, "select", "queued")
    send(tap(f"q:{job_id}"))
    send(tap(f"q:{uuid.uuid4()}"))
    assert requeued == [] and all("not dead" in c.fields["text"] for c in fake.calls_to("answerCallbackQuery"))


def test_only_you_can_requeue(chat, db):
    send, fake, requeued, profile = chat
    job_id = make_job(db, profile, "select", "dead")
    send(tap(f"q:{job_id}", user=STRANGER))
    with db.connect() as conn:
        assert jobs.get(conn, job_id)["status"] == "dead"  # type: ignore[index]
    assert requeued == [] and fake.calls == []


# ---------- the commands room -------------------------------------------------------------------------------------
GROUP = -1001234567890


def in_topic(text, thread, user=ME, chat=GROUP):
    return {
        "update_id": 1,
        "message": {
            "message_id": 9, "message_thread_id": thread, "from": {"id": user}, "text": text,
            "chat": {"id": chat, "type": "supergroup", "is_forum": True},
        },
    }  # fmt: skip


@pytest.fixture
def room(db, world):
    from common.rooms import Rooms

    ctx, _, _ = world
    with fake_telegram() as (fake, url):
        bot = Telegram(TOKEN, base_url=url)
        rooms = Rooms(GROUP, {"approval": 11, "alerts": 12, "monitoring": 13, "commands": 14})

        def send(update):
            handle_update(update, engine=db, bot=bot, allowed={ME}, publish_job=None, commands=ctx, rooms=rooms)

        yield send, fake


def test_a_command_in_the_commands_topic_is_answered_in_that_topic(room):
    send, fake = room
    send(in_topic("/status", 14))
    (reply,) = fake.calls_to("sendMessage")
    assert reply.fields["chat_id"] == GROUP and reply.fields["message_thread_id"] == 14


def test_a_command_in_any_other_topic_or_group_or_from_anyone_else_is_ignored(room):
    send, fake = room
    send(in_topic("/status", 11))  # the approval topic
    send(in_topic("/status", 12))
    send(in_topic("/status", 14, chat=-100999))  # another group's topic with the same number
    send(in_topic("/status", 14, user=STRANGER))
    send(in_topic("/status", None))  # the General topic
    assert fake.calls == []


def test_private_chat_commands_still_work_when_a_group_is_set_up(room):
    send, fake = room
    send(message("/help"))
    assert fake.calls_to("sendMessage")[0].fields["chat_id"] == ME


# ---------- /health -----------------------------------------------------------------------------------------------
def test_health_says_for_each_expected_service_whether_it_is_up_down_or_unseen(db, world):
    _, _, tmp = world
    ctx = cmd.Commands(
        db, LIMITS, accounts(tmp, []), now=lambda: NOW, expected=["select", "render", "package"], stale_after_s=120
    )
    from common import heartbeat

    with db.begin() as conn:
        heartbeat.beat(conn, "select", "b1")
        heartbeat.beat(conn, "render", "b1")
        conn.execute(
            sa.text("UPDATE heartbeats SET beat_at = clock_timestamp() - interval '9 minutes' WHERE service = 'render'")
        )
    text = ctx.health()
    assert "select" in text and "up" in text
    assert "render: DOWN" in text and "9 min" in text
    assert "package: never seen" in text


def test_status_has_a_one_line_summary_of_the_services(db, world):
    _, _, tmp = world
    ctx = cmd.Commands(db, LIMITS, accounts(tmp, []), now=lambda: NOW, expected=["select", "render"], stale_after_s=120)
    from common import heartbeat

    with db.begin() as conn:
        heartbeat.beat(conn, "select", "b1")
    assert "Services: 1 of 2 up (not up: render)" in ctx.status()


def test_without_the_list_of_services_health_says_it_is_not_set_up(db, world):
    assert "not set up" in world[0].health()


def test_health_is_a_command_and_in_the_menu():
    assert cmd.parse("/health") == "health" and "health" in [name for name, _ in cmd.MENU]


def test_status_names_each_hand_switch_that_is_on(db, world):
    from common import pauses

    ctx, _, _ = world
    with db.begin() as conn:
        pauses.hold(conn, "switch:live", "t")
        pauses.hold(conn, "switch:ended", "t")
    text = ctx.status()
    assert "live recordings" in text and "ended-stream discovery" in text
