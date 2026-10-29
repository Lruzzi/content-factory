"""/rules, /cap, /spacing, /window and /clips in the chat: see the posting rules and change them from Telegram. A change
is parsed strictly, asks for a Yes tap, can never go above the ceilings in workers.yaml, and only an allowed user can
make it. What you set is kept in the database and read by the publishers and the select stage."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fake_telegram_server import fake_telegram

from common import rules
from common.telegram import Telegram
from services.publisher import accounts as acc
from services.review import commands as cmd
from services.review.listener import handle_update
from services.understand import budget

pytestmark = pytest.mark.integration

ME, STRANGER = 111, 222
TOKEN = "123456789:FAKE-token_for_tests"  # noqa: S105  # gitleaks:allow
GROQ = budget.Limits(audio_seconds_per_hour=7200, audio_seconds_per_day=28800, alert_at=0.8, stop_at=0.9)
CEIL = rules.Ceilings(daily_cap=20, min_spacing_min=720, clips_per_day=40)


def accounts(tmp_path: Path) -> acc.Accounts:
    path = tmp_path / "accounts.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "timezone": "Asia/Jakarta",
                "defaults": {"daily_cap": 6, "min_spacing_min": 120, "post_windows": ["10:00-23:00"]},
                "accounts": [{"id": "yt_test", "platform": "shorts"}, {"id": "fb_test", "platform": "facebook"}],
            }
        )
    )
    return acc.load(path)


@pytest.fixture
def chat(db, tmp_path):
    ctx = cmd.Commands(db, GROQ, accounts(tmp_path), rule_ceilings=CEIL, clips_default=6)
    with fake_telegram() as (fake, url):
        bot = Telegram(TOKEN, base_url=url)

        def send(update):
            handle_update(update, engine=db, bot=bot, allowed={ME}, publish_job=None, commands=ctx)

        yield send, fake


def message(text, user=ME):
    return {"update_id": 1, "message": {"from": {"id": user}, "chat": {"id": user, "type": "private"}, "text": text}}


def tap(data, user=ME):
    query = {"id": "cb", "from": {"id": user}, "data": data, "message": {"message_id": 77, "chat": {"id": user}}}
    return {"update_id": 2, "callback_query": query}


def buttons(call):
    return [b["callback_data"] for row in call.fields["reply_markup"]["inline_keyboard"] for b in row]


def stored(db):
    with db.connect() as conn:
        return rules.overrides(conn)


# ---------- seeing the rules ----------
def test_rules_shows_what_is_in_force_and_how_to_change_it(chat):
    send, fake = chat
    send(message("/rules"))
    text = fake.calls_to("sendMessage")[0].fields["text"]
    assert "Cap: 6 posts per account in 24 h (from config)" in text and "Spacing: 120 min" in text
    assert "Window: 10:00-23:00 (Asia/Jakarta)" in text and "Clips made: up to 6 in 24 h, 0 so far" in text
    assert "/cap 6" in text and "confirm" in text


@pytest.mark.parametrize("word", ["/cap", "/spacing", "/window", "/clips"])
def test_a_bare_rule_word_shows_the_rules_too(chat, word):
    send, fake = chat
    send(message(word))
    assert "Posting rules" in fake.calls_to("sendMessage")[0].fields["text"]


# ---------- asking before changing ----------
@pytest.mark.parametrize(
    ("text", "token", "words"),
    [
        ("/cap 4", "rl:cap:4", "posts per account in 24 hours to 4"),
        ("/spacing 90", "rl:spacing:90", "minutes between posts to 90"),
        ("/clips 8", "rl:clips:8", "clips made in 24 hours (0: no limit) to 8"),
        ("/clips 0", "rl:clips:0", "to 0"),
        ("/window 09:00-12:00", "rl:window:0900-1200", "posting window to 09:00-12:00"),
        ("/window 10:00-13:00,18:00-23:00", "rl:window:1000-1300,1800-2300", "10:00-13:00, 18:00-23:00"),
        ("/window any", "rl:window:any", "any time"),
        ("/cap@MyFactoryBot 5", "rl:cap:5", "to 5"),
    ],
)
def test_a_change_asks_first_and_changes_nothing_yet(chat, db, text, token, words):
    send, fake = chat
    send(message(text))
    (ask,) = fake.calls_to("sendMessage")
    assert words in ask.fields["text"] and buttons(ask) == [token, "pc"] and stored(db) == {}


@pytest.mark.parametrize(
    "text",
    ["/cap 0", "/cap 21", "/cap six", "/cap 4.5", "/spacing 721", "/clips 41", "/window 10-23", "/window 25:00-26:00"],
)
def test_a_value_the_ceilings_or_the_format_refuse_is_not_even_asked(chat, db, text):
    send, fake = chat
    send(message(text))
    (reply,) = fake.calls_to("sendMessage")
    assert "reply_markup" not in reply.fields and stored(db) == {}


@pytest.mark.parametrize("text", ["please /cap 4", "/cap 4 now", "/cap", "cap 4", "/cap 4; DROP TABLE settings"])
def test_anything_but_the_exact_form_is_not_read_as_a_change(chat, db, text):
    send, fake = chat
    send(message(text))
    assert all("reply_markup" not in c.fields for c in fake.calls_to("sendMessage")) and stored(db) == {}


# ---------- confirming ----------
def test_confirming_keeps_the_rule_remembers_who_and_shows_the_new_rules(chat, db):
    send, fake = chat
    send(tap("rl:cap:4"))
    assert stored(db)["cap"] == (4, f"telegram:{ME}")
    (edit,) = fake.calls_to("editMessageText")
    assert f"Cap: 4 posts per account in 24 h (set by telegram:{ME})" in edit.fields["text"]


def test_a_window_is_kept_as_ranges(chat, db):
    send, _ = chat
    send(tap("rl:window:1000-1300,1800-2300"))
    assert stored(db)["window"][0] == ["10:00-13:00", "18:00-23:00"]
    send(tap("rl:window:any"))
    assert stored(db)["window"][0] == []


@pytest.mark.parametrize("data", ["rl:cap:21", "rl:cap:0", "rl:spacing:9999", "rl:clips:41"])
def test_a_tap_with_a_value_the_ceilings_refuse_changes_nothing(chat, db, data):
    send, fake = chat
    send(tap(data))
    assert stored(db) == {} and fake.calls_to("editMessageText") == []


@pytest.mark.parametrize("data", ["rl:nope:4", "rl:cap:", "rl:cap:4;5", "rl:window:10:00-13:00", "rl:cap:" + "9" * 200])
def test_a_button_code_that_is_not_ours_is_ignored(chat, db, data):
    send, fake = chat
    send(tap(data))
    assert stored(db) == {} and fake.calls_to("answerCallbackQuery") == []


def test_only_an_allowed_user_can_change_or_ask(chat, db):
    send, fake = chat
    send(message("/cap 4", user=STRANGER))
    send(tap("rl:cap:4", user=STRANGER))
    assert fake.calls_to("sendMessage") == [] and stored(db) == {}


def test_a_change_shows_up_in_what_the_publishers_apply(chat, db, tmp_path):
    send, _ = chat
    send(tap("rl:cap:3"))
    send(tap("rl:spacing:60"))
    with db.connect() as conn:
        rules_now = rules.apply(conn, accounts(tmp_path).by_id["fb_test"])
    assert (rules_now.daily_cap, rules_now.min_spacing_min) == (3, 60) and rules_now.post_windows == ["10:00-23:00"]


def test_the_bots_menu_lists_rules(chat):
    assert any(name == "rules" for name, _ in cmd.MENU)
