"""Pause and resume from the chat: posting, intake (new recordings and discovery) and single sources. A pause asks for a
confirm, is kept in Postgres so a restart does not undo it, and resuming is one tap. A source paused after a copyright
claim cannot be resumed from here (FR-10)."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from fake_telegram_server import fake_telegram

import config_schema as cs
from common import pauses, sources
from common.telegram import Telegram
from services.publisher import accounts as acc
from services.review import commands as cmd
from services.review.listener import handle_update
from services.understand import budget

pytestmark = pytest.mark.integration

ME, STRANGER = 111, 222
TOKEN = "123456789:FAKE-token_for_tests"  # noqa: S105  # gitleaks:allow
LIMITS = budget.Limits(audio_seconds_per_hour=7200, audio_seconds_per_day=28800, alert_at=0.8, stop_at=0.9)


def accounts(tmp_path: Path) -> acc.Accounts:
    path = tmp_path / "accounts.yaml"
    path.write_text(yaml.safe_dump({"timezone": "UTC", "accounts": [{"id": "yt_test", "platform": "shorts"}]}))
    return acc.load(path)


@pytest.fixture
def chat(db, tmp_path):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
    ctx = cmd.Commands(db, LIMITS, accounts(tmp_path))
    with fake_telegram() as (fake, url):
        bot = Telegram(TOKEN, base_url=url)

        def send(update):
            handle_update(update, engine=db, bot=bot, allowed={ME}, publish_job=None, commands=ctx)

        setattr(send, "ctx", ctx)  # noqa: B010
        yield send, fake


def message(text, user=ME):
    return {"update_id": 1, "message": {"from": {"id": user}, "chat": {"id": user, "type": "private"}, "text": text}}


def tap(data, user=ME):
    query = {"id": "cb", "from": {"id": user}, "data": data, "message": {"message_id": 77, "chat": {"id": user}}}
    return {"update_id": 2, "callback_query": query}


def buttons(call):
    return [b["callback_data"] for row in call.fields["reply_markup"]["inline_keyboard"] for b in row]


def source_row(db, key="leokabuki"):
    with db.connect() as conn:
        return conn.execute(sa.text("SELECT * FROM sources WHERE source_key = :k"), {"k": key}).mappings().one()


# ---------- /pause ----------
def test_pause_offers_posting_intake_and_each_source_that_is_running(chat):
    send, fake = chat
    send(message("/pause"))
    (menu,) = fake.calls_to("sendMessage")
    offered = buttons(menu)
    assert {"ps:publish", "ps:live", "ps:ended", "ps:intake"} <= set(offered)
    assert "ps:src:leokabuki" in offered and "ps:src:jejeadriel20" in offered


def test_pressing_a_pause_button_asks_first_and_changes_nothing_yet(chat, db):
    send, fake = chat
    send(tap("ps:publish"))
    ask = fake.calls_to("editMessageText")[0].fields
    assert "Pause posting" in ask["text"] and "pz:publish" in str(ask) and "pc" in str(ask)
    with db.connect() as conn:
        assert pauses.switches_on(conn) == []


def test_confirming_pauses_posting_and_is_kept_in_the_database(chat, db):
    send, fake = chat
    send(tap("pz:publish"))
    with db.connect() as conn:
        assert pauses.switches_on(conn) == ["publish"]
        held = pauses.active(conn, "switch:publish")
    assert held is not None and str(ME) in held.reason
    assert "paused" in fake.calls_to("answerCallbackQuery")[0].fields["text"].lower()
    assert "Posting is paused" in fake.calls_to("editMessageText")[0].fields["text"]


def test_confirming_the_same_pause_twice_says_it_already_is(chat, db):
    send, fake = chat
    send(tap("pz:intake"))
    send(tap("pz:intake"))
    assert "already" in fake.calls_to("answerCallbackQuery")[1].fields["text"].lower()


def test_cancel_leaves_everything_as_it_was(chat, db):
    send, fake = chat
    send(tap("pc"))
    with db.connect() as conn:
        assert pauses.switches_on(conn) == []
    assert "cancel" in fake.calls_to("editMessageText")[0].fields["text"].lower()


def test_confirming_a_source_pauses_it_as_manual_by_you(chat, db):
    send, _ = chat
    send(tap("pz:src:leokabuki"))
    row = source_row(db)
    assert row["paused_at"] is not None and row["pause_reason"] == "manual" and row["updated_by"] == f"telegram:{ME}"
    assert source_row(db, "jejeadriel20")["paused_at"] is None


def test_a_source_that_does_not_exist_is_refused(chat):
    send, fake = chat
    send(tap("pz:src:nobody"))
    assert "no such source" in fake.calls_to("answerCallbackQuery")[0].fields["text"].lower()


# ---------- /resume ----------
def test_resume_offers_only_what_is_paused_and_one_tap_resumes_it(chat, db):
    send, fake = chat
    send(tap("pz:publish"))
    send(tap("pz:src:leokabuki"))
    fake.calls.clear()
    send(message("/resume"))
    offered = buttons(fake.calls_to("sendMessage")[0])
    assert sorted(offered) == ["rs:publish", "rs:src:leokabuki"]
    send(tap("rs:publish"))
    send(tap("rs:src:leokabuki"))
    with db.connect() as conn:
        assert pauses.switches_on(conn) == []
    assert source_row(db)["paused_at"] is None


def test_after_a_resume_tap_the_message_says_so_and_the_button_is_gone(chat):
    send, fake = chat
    send(tap("pz:publish"))
    fake.calls.clear()
    send(tap("rs:publish"))
    (edit,) = fake.calls_to("editMessageText")
    assert "resumed" in edit.fields["text"].lower() and edit.fields["reply_markup"]["inline_keyboard"] == []


def test_resume_with_nothing_paused_says_so(chat):
    send, fake = chat
    send(message("/resume"))
    assert "Nothing is paused" in fake.calls_to("sendMessage")[0].fields["text"]


def test_a_source_paused_after_a_copyright_claim_cannot_be_resumed_from_here(chat, db):
    send, fake = chat
    with db.begin() as conn:
        sources.record_claim(conn, source_row(db)["id"])
    assert source_row(db)["pause_reason"] == "claim"
    send(message("/resume"))
    text = fake.calls_to("sendMessage")[0]
    assert "rs:src:leokabuki" not in str(text.fields.get("reply_markup")) and "claim" in text.fields["text"].lower()
    send(tap("rs:src:leokabuki"))
    assert source_row(db)["pause_reason"] == "claim"
    assert "claim" in fake.calls_to("answerCallbackQuery")[0].fields["text"].lower()


def test_a_source_that_is_already_paused_is_not_offered_to_pause_again(chat, db):
    send, fake = chat
    send(tap("pz:src:leokabuki"))
    fake.calls.clear()
    send(message("/pause"))
    assert "ps:src:leokabuki" not in buttons(fake.calls_to("sendMessage")[0])


# ---------- who may ----------
@pytest.mark.parametrize("data", ["ps:publish", "pz:publish", "pz:src:leokabuki", "rs:publish", "pc"])
def test_a_stranger_cannot_pause_or_resume_anything(chat, db, data):
    send, fake = chat
    send(tap(data, user=STRANGER))
    with db.connect() as conn:
        assert pauses.switches_on(conn) == []
    assert fake.calls == [] and source_row(db)["paused_at"] is None


@pytest.mark.parametrize(
    "data", ["pz:", "pz:everything", "pz:src:", "pz:src:a b", "pz:src:" + "x" * 80, "rs:src:../x", "zz:publish"]
)
def test_a_button_with_anything_else_in_it_is_ignored(chat, db, data):
    send, fake = chat
    send(tap(data))
    with db.connect() as conn:
        assert pauses.switches_on(conn) == []
    assert fake.calls == []


def test_status_lists_what_is_paused(chat, db):
    send, fake = chat
    send(tap("pz:publish"))
    send(tap("pz:src:leokabuki"))
    ctx = send.ctx
    assert "Paused: posting, source leokabuki (manual)" in ctx.status()


def test_live_and_ended_are_paused_and_resumed_each_on_its_own(chat, db):
    send, _ = chat
    send(tap("pz:live"))
    with db.connect() as conn:
        assert pauses.switches_on(conn) == ["live"]
    send(tap("pz:ended"))
    with db.connect() as conn:
        assert pauses.switches_on(conn) == ["ended", "live"]
    send(tap("rs:live"))
    with db.connect() as conn:
        assert pauses.switches_on(conn) == ["ended"]


def test_the_question_for_each_switch_says_what_it_stops(chat):
    send, fake = chat
    send(tap("ps:live"))
    send(tap("ps:ended"))
    live, ended = (c.fields["text"] for c in fake.calls_to("editMessageText"))
    assert "live stream" in live and "Ended streams are still looked for" in live
    assert "No ended stream" in ended and "still recorded" in ended
