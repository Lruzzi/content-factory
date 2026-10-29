"""/budget in the chat: see the month's OpenRouter budget and change the cap by sending `/budget 25` (it asks, then a
tap confirms). The cap lives in the database, can never go above the typo guard in workers.yaml, and only an allowed
user can change it. The amount is parsed strictly: it is the one thing besides a command word the bot reads."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from fake_telegram_server import fake_telegram

from common import ledger, spend
from common.telegram import Telegram
from services.publisher import accounts as acc
from services.review import commands as cmd
from services.review.listener import handle_update
from services.understand import budget

pytestmark = pytest.mark.integration

ME, STRANGER = 111, 222
TOKEN = "123456789:FAKE-token_for_tests"  # noqa: S105  # gitleaks:allow
GROQ = budget.Limits(audio_seconds_per_hour=7200, audio_seconds_per_day=28800, alert_at=0.8, stop_at=0.9)
SPEND = spend.Limits(cap_ceiling_usd=100.0, cap_usd=10.0, alert_at=0.8, stop_at=0.9)


def accounts(tmp_path: Path) -> acc.Accounts:
    path = tmp_path / "accounts.yaml"
    path.write_text(yaml.safe_dump({"timezone": "UTC", "accounts": [{"id": "yt_test", "platform": "shorts"}]}))
    return acc.load(path)


@pytest.fixture
def chat(db, tmp_path):
    ctx = cmd.Commands(db, GROQ, accounts(tmp_path), spend_limits=SPEND)
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


def spent(db, dollars):
    with db.begin() as conn:
        ledger.record_cost(
            conn, profile="mlbb", service="select", provider="openai_compatible", units=Decimal(1),
            unit="input_tokens", usd=Decimal(str(dollars)),
        )  # fmt: skip


def cap_of(db):
    with db.connect() as conn:
        return spend.load(conn, SPEND).cap_usd


def test_budget_shows_the_cap_the_spend_the_stop_and_what_is_left_per_day(chat, db):
    send, fake = chat
    spent(db, 4.0)
    send(message("/budget"))
    (reply,) = fake.calls_to("sendMessage")
    text = reply.fields["text"]
    assert "Cap: $10.00" in text and "Spent: $4.00 (40%)" in text and "stops at $9.00 (90%)" in text
    assert "Left per day: $" in text and "running" in text.lower()
    assert "/budget 25" in text and "reply_markup" not in reply.fields


def test_budget_says_stopped_when_the_spend_is_past_the_stop(chat, db):
    send, fake = chat
    spent(db, 9.5)
    send(message("/budget"))
    assert "STOPPED" in fake.calls_to("sendMessage")[0].fields["text"]


def test_sending_an_amount_asks_before_changing_anything(chat, db):
    send, fake = chat
    send(message("/budget 25"))
    (ask,) = fake.calls_to("sendMessage")
    assert "$10.00" in ask.fields["text"] and "$25.00" in ask.fields["text"]
    assert buttons(ask) == ["bz:25.00", "pc"] and cap_of(db) == 10.0


@pytest.mark.parametrize("text", ["/budget 12.5", "/budget@MyFactoryBot 12.50", "  /budget 12.50  "])
def test_the_amount_may_have_cents_and_the_bot_name(chat, text):
    send, fake = chat
    send(message(text))
    assert buttons(fake.calls_to("sendMessage")[0]) == ["bz:12.50", "pc"]


def test_confirming_changes_the_cap_remembers_who_and_shows_the_new_numbers(chat, db):
    send, fake = chat
    send(tap("bz:25.00"))
    assert cap_of(db) == 25.0
    (edit,) = fake.calls_to("editMessageText")
    assert "Cap: $25.00" in edit.fields["text"]
    with db.connect() as conn:
        assert conn.execute(sa.text("SELECT updated_by FROM settings")).scalar_one() == f"telegram:{ME}"


def test_the_cap_may_go_above_what_it_started_at_up_to_the_typo_guard(chat, db):
    send, fake = chat
    send(message("/budget 99.99"))
    assert buttons(fake.calls_to("sendMessage")[0]) == ["bz:99.99", "pc"]


@pytest.mark.parametrize("text", ["/budget 0", "/budget 0.00", "/budget 100.01", "/budget 99999"])
def test_a_cap_of_zero_or_above_the_typo_guard_is_refused_without_asking(chat, db, text):
    send, fake = chat
    send(message(text))
    (reply,) = fake.calls_to("sendMessage")
    assert "cap must be" in reply.fields["text"] and "reply_markup" not in reply.fields and cap_of(db) == 10.0


@pytest.mark.parametrize("data", ["bz:100.01", "bz:0", "bz:0.00"])
def test_a_tap_with_a_cap_the_guard_refuses_changes_nothing(chat, db, data):
    send, fake = chat
    send(tap(data))
    assert "cap must be" in fake.calls_to("answerCallbackQuery")[0].fields["text"] and cap_of(db) == 10.0


@pytest.mark.parametrize(
    "text",
    ["/budget abc", "/budget 1e9", "/budget -5", "/budget 1.234", "/budget 5;drop", "/budget " + "9" * 40,
     "/budget 5 6",
     "/budget  ", "budget 5", "please /budget 5", "/budget $5", "/budget 5\n/pause"],
)  # fmt: skip
def test_anything_that_is_not_a_plain_amount_after_the_command_is_not_read_as_one(chat, db, text):
    send, fake = chat
    send(message(text))
    assert all("bz:" not in str(c.fields) for c in fake.calls) and cap_of(db) == 10.0


@pytest.mark.parametrize(
    "data", ["bz:abc", "bz:1e9", "bz:-5", "bz:", "bz:1.234", "bq:5", "bz:5;drop", "bz:" + "9" * 40]
)
def test_anything_that_is_not_a_plain_amount_in_a_tap_is_ignored(chat, db, data):
    send, fake = chat
    send(tap(data))
    assert fake.calls == [] and cap_of(db) == 10.0


def test_a_stranger_cannot_ask_or_change_the_cap(chat, db):
    send, fake = chat
    send(message("/budget 25", user=STRANGER))
    send(tap("bz:12.00", user=STRANGER))
    assert fake.calls == [] and cap_of(db) == 10.0


def test_without_a_budget_section_the_command_says_so(db, tmp_path):
    ctx = cmd.Commands(db, GROQ, accounts(tmp_path))
    with fake_telegram() as (fake, url):
        handle_update(
            message("/budget"), engine=db, bot=Telegram(TOKEN, base_url=url), allowed={ME}, publish_job=None,
            commands=ctx,
        )  # fmt: skip
        assert "not set up" in fake.calls_to("sendMessage")[0].fields["text"]


def test_budget_is_in_the_menu_and_the_help():
    assert cmd.parse("/budget") == "budget" and "budget" in [name for name, _ in cmd.MENU]
    assert "/budget" in cmd.help_text()


def test_status_shows_the_budget_line_and_puts_a_stop_in_the_summary(chat, db):
    send, fake = chat
    spent(db, 2.0)
    assert "OpenRouter: $2.00 of $10.00 (20%)" in chat[0].ctx.status()
    spent(db, 8.0)
    assert "OpenRouter budget stopped" in chat[0].ctx.status().splitlines()[1]


def test_budget_says_how_the_audio_is_planned_for_today(db, tmp_path):
    from services.understand import planner

    cfg = planner.Config(enabled=True, assumed_cost_per_audio_hour_usd=0.03, min_measured_hours=2)
    ctx = cmd.Commands(db, GROQ, accounts(tmp_path), spend_limits=SPEND, planner_cfg=cfg)
    text = ctx.budget_text()
    assert "Audio plan: Groq allows 7.2 h a day, shared by 1 active source (7.2 h each)" in text
    assert "pays for about" in text and "$0.030 per hour of audio" in text


def test_without_a_planner_budget_has_no_audio_plan_lines(chat):
    send, fake = chat
    send(message("/budget"))
    assert "Audio plan" not in fake.calls_to("sendMessage")[0].fields["text"]
