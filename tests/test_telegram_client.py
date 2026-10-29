"""The Telegram client: what it sends, and that the bot token never leaks into an error or a log (FR-8)."""

from __future__ import annotations

import json
import logging

import pytest
from fake_telegram_server import fake_telegram

from common.telegram import Telegram, TelegramError

TOKEN = "123456789:FAKE-token_for_tests"  # noqa: S105  # gitleaks:allow


@pytest.fixture
def bot(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not really a video" * 100)
    with fake_telegram() as (fake, url):
        yield Telegram(TOKEN, base_url=url), fake, video


def test_a_video_is_sent_with_a_plain_text_caption_and_inline_buttons(bot):
    client, fake, video = bot
    message_id = client.send_video(42, video, "Judul\nkind: donation", [[("Approve", "a:1"), ("Reject", "r:1")]])
    (call,) = fake.calls_to("sendVideo")
    assert message_id > 100 and call.token == TOKEN
    assert call.fields["chat_id"] == "42" and call.fields["caption"] == "Judul\nkind: donation"
    assert "parse_mode" not in call.fields  # plain text: nothing in a caption can be read as formatting
    assert json.loads(call.fields["reply_markup"]) == {
        "inline_keyboard": [[{"text": "Approve", "callback_data": "a:1"}, {"text": "Reject", "callback_data": "r:1"}]]
    }
    assert call.files["video"] == video.read_bytes() and call.fields["supports_streaming"] == "true"


def test_a_document_is_sent_as_it_is_so_telegram_does_not_recompress_it(bot):
    client, fake, video = bot
    message_id = client.send_document(42, video, "TikTok: Judul")
    (call,) = fake.calls_to("sendDocument")
    assert message_id > 100 and call.fields["chat_id"] == "42" and call.fields["caption"] == "TikTok: Judul"
    assert call.files["document"] == video.read_bytes() and "parse_mode" not in call.fields
    assert "reply_markup" not in call.fields and call.fields["disable_content_type_detection"] == "true"


def test_a_message_can_carry_buttons_and_a_plain_one_has_none(bot):
    client, fake, _ = bot
    client.send_message(42, "Download: https://example.test/x", buttons=[[("Delete file", "x:1")]])
    client.send_message(42, "just text")
    with_buttons, plain = fake.calls_to("sendMessage")
    assert with_buttons.fields["reply_markup"] == {
        "inline_keyboard": [[{"text": "Delete file", "callback_data": "x:1"}]]
    }
    assert "reply_markup" not in plain.fields


def test_the_text_of_a_message_can_be_replaced_and_its_buttons_taken_away(bot):
    client, fake, _ = bot
    client.edit_message_text(42, 101, "Deleted")
    edit = fake.calls_to("editMessageText")[0].fields
    assert (edit["chat_id"], edit["message_id"], edit["text"]) == (42, 101, "Deleted")
    assert edit["reply_markup"] == {"inline_keyboard": []}


def test_the_commands_menu_is_registered_with_the_bot(bot):
    client, fake, _ = bot
    client.set_my_commands([("status", "Queue and quotas"), ("dead", "Failed jobs")])
    assert fake.calls_to("setMyCommands")[0].fields["commands"] == [
        {"command": "status", "description": "Queue and quotas"},
        {"command": "dead", "description": "Failed jobs"},
    ]


def test_a_caption_longer_than_telegram_allows_is_cut_not_refused(bot):
    client, fake, video = bot
    client.send_video(42, video, "x" * 3000, [])
    caption = fake.calls_to("sendVideo")[0].fields["caption"]
    assert len(caption) == 1024 and caption.endswith("…")


def test_updates_are_asked_for_after_an_offset_and_only_for_button_taps(bot):
    client, fake, _ = bot
    fake.updates = [{"update_id": 7, "callback_query": {"id": "c1"}}]
    assert client.get_updates(offset=7, timeout_s=0) == [{"update_id": 7, "callback_query": {"id": "c1"}}]
    fields = fake.calls_to("getUpdates")[0].fields
    assert fields["offset"] == 7 and fields["timeout"] == 0
    assert fields["allowed_updates"] == ["callback_query", "message"]  # messages are read only for exact commands


def test_a_tap_is_answered_and_the_buttons_are_taken_away_with_a_new_caption(bot):
    client, fake, _ = bot
    client.answer_callback("c1", "Approved")
    client.edit_caption(42, 101, "Approved by you")
    assert fake.calls_to("answerCallbackQuery")[0].fields == {"callback_query_id": "c1", "text": "Approved"}
    edit = fake.calls_to("editMessageCaption")[0].fields
    assert edit["chat_id"] == 42 and edit["message_id"] == 101 and edit["caption"] == "Approved by you"
    assert edit["reply_markup"] == {"inline_keyboard": []}


def test_a_caption_can_be_replaced_with_new_buttons_instead_of_none(bot):
    client, fake, _ = bot
    client.edit_caption(42, 101, "Rejected: why?", [[("Boring", "w:boring:x"), ("Skip", "w:skip:x")]])
    keyboard = fake.calls_to("editMessageCaption")[0].fields["reply_markup"]["inline_keyboard"]
    assert [b["callback_data"] for b in keyboard[0]] == ["w:boring:x", "w:skip:x"]


def test_an_error_from_telegram_says_why_and_never_contains_the_token(bot):
    client, fake, video = bot
    fake.errors["sendVideo"] = (403, "Forbidden: bot was blocked by the user")
    with pytest.raises(TelegramError, match="blocked by the user") as caught:
        client.send_video(42, video, "x", [])
    assert TOKEN not in str(caught.value) and "bot123" not in str(caught.value)


def test_an_unreachable_server_is_an_error_that_does_not_carry_the_url_with_the_token(tmp_path):
    client = Telegram(TOKEN, base_url="http://127.0.0.1:9", timeout_s=2)
    with pytest.raises(TelegramError) as caught:
        client.get_updates(offset=None, timeout_s=0)
    assert TOKEN not in str(caught.value) and caught.value.__cause__ is None and caught.value.__suppress_context__


def test_the_token_never_appears_in_the_logs(bot, caplog):
    client, fake, video = bot
    with caplog.at_level(logging.DEBUG):
        client.send_video(42, video, "x", [])
        client.get_updates(offset=None, timeout_s=0)
        fake.errors["answerCallbackQuery"] = (400, "Bad Request: query is too old")
        with pytest.raises(TelegramError):
            client.answer_callback("c1")
    assert TOKEN not in caplog.text and "FAKE-token" not in caplog.text


@pytest.mark.parametrize("bad", ["", "no-colon", "12:ab/../x", "abc:def", "123456789:tok en"])
def test_a_token_that_could_change_the_request_path_is_refused(bad):
    with pytest.raises(ValueError, match="token"):
        Telegram(bad)


def test_a_plain_text_message_is_sent_without_any_formatting_and_cut_to_telegrams_limit(bot):
    client, fake, _ = bot
    message_id = client.send_message(42, "x" * 5000)
    (call,) = fake.calls_to("sendMessage")
    assert message_id > 100 and call.fields["chat_id"] == 42 and "parse_mode" not in call.fields
    assert len(call.fields["text"]) == 4096 and call.fields["text"].endswith("…")


# ---------- link buttons ----------
def test_a_button_whose_data_is_a_web_address_opens_it_instead_of_sending_a_callback(bot):
    client, fake, _ = bot
    link = "https://files.example/review/x.mp4?X-Amz-Signature=abc"
    client.send_message(42, "hi", [[("✅ Approve", "a:1"), ("❌ Reject", "r:1")], [("⬇ Full quality", link)]])
    keyboard = fake.calls_to("sendMessage")[0].fields["reply_markup"]["inline_keyboard"]
    assert keyboard[0] == [
        {"text": "✅ Approve", "callback_data": "a:1"},
        {"text": "❌ Reject", "callback_data": "r:1"},
    ]
    assert keyboard[1] == [{"text": "⬇ Full quality", "url": link}]


def test_link_buttons_work_on_a_video_and_when_a_caption_is_edited(bot, tmp_path):
    client, fake, _ = bot
    link = "https://files.example/review/x.mp4?sig=1"
    clip = tmp_path / "v.mp4"
    clip.write_bytes(b"x")
    client.send_video(42, clip, "cap", [[("A", "a:1")], [("⬇ Full", link)]])
    markup = json.loads(fake.calls_to("sendVideo")[0].fields["reply_markup"])
    assert markup["inline_keyboard"][1] == [{"text": "⬇ Full", "url": link}]
    client.edit_caption(42, 5, "Approved", [[("⬇ Full", link)]])
    assert fake.calls_to("editMessageCaption")[0].fields["reply_markup"]["inline_keyboard"] == [
        [{"text": "⬇ Full", "url": link}]
    ]


def test_only_https_addresses_and_this_machines_own_become_link_buttons(bot):
    client, fake, _ = bot
    client.send_message(
        42, "x", [[("a", "http://evil.example/x"), ("b", "javascript:1"), ("c", "x:1"), ("d", "http://127.0.0.1:9/x")]]
    )
    row = fake.calls_to("sendMessage")[0].fields["reply_markup"]["inline_keyboard"][0]
    assert [list(b) for b in row] == [["text", "callback_data"]] * 3 + [["text", "url"]]
