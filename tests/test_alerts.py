"""Alerts reach your phone through Telegram, and can never crash the service that raised them."""

from __future__ import annotations

import logging

import pytest
from fake_telegram_server import fake_telegram

from common import alerts
from common.telegram import Telegram

TOKEN = "123456789:FAKE-token_for_tests"  # noqa: S105  # gitleaks:allow


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def tg():
    with fake_telegram() as (fake, url):
        yield fake, Telegram(TOKEN, base_url=url)


def alerter(tg, allowed="111,222", clock=None):
    fake, bot = tg
    return alerts.make_alerter(
        "render", {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ALLOWED_USERS": allowed}, bot=bot, clock=clock or Clock()
    )


def sent(tg):
    return [(c.fields["chat_id"], c.fields["text"]) for c in tg[0].calls_to("sendMessage")]


def test_an_alert_goes_to_every_allowed_user_with_the_name_of_the_service(tg):
    alerter(tg)("job 1 is dead at stage render: ffmpeg crashed")
    assert sent(tg) == [(111, "⚠️ render: job 1 is dead at stage render: ffmpeg crashed")] + [
        (222, "⚠️ render: job 1 is dead at stage render: ffmpeg crashed")
    ]


def test_text_that_came_from_a_platform_or_a_model_is_cleaned_and_cut(tg):
    alerter(tg)("bad\x00 reply " + chr(0x202E) + "evil\n" + "x" * 3000)
    (_, text), _ = sent(tg)
    assert (
        "\x00" not in text
        and chr(0x202E) not in text
        and len(text) <= 1000 + len("\u26a0\ufe0f render: ")
        and "\n" in text
    )  # the body is cut to 1000


def test_every_alert_is_also_written_to_the_log(tg, caplog):
    with caplog.at_level(logging.ERROR):
        alerter(tg)("something broke")
    assert "ALERT render: something broke" in caplog.text


def test_without_telegram_settings_an_alert_is_only_logged(caplog):
    with caplog.at_level(logging.ERROR):
        alerts.make_alerter("render", {})("something broke")
    assert "something broke" in caplog.text


def test_a_failing_send_is_logged_and_the_other_users_still_get_the_alert(tg, caplog):
    tg[0].errors["sendMessage"] = (403, "Forbidden: bot was blocked by the user")
    with caplog.at_level(logging.WARNING):
        alerter(tg)("something broke")  # must not raise
    assert len(tg[0].calls_to("sendMessage")) == 2 and "blocked" in caplog.text


def test_the_same_alert_is_not_repeated_within_half_an_hour(tg):
    clock = Clock()
    alert = alerter(tg, clock=clock)
    alert("sign in again")
    alert("sign in again")
    alert("something else")
    assert len(sent(tg)) == 4  # two messages x two users
    clock.now += 1801
    alert("sign in again")
    assert len(sent(tg)) == 6


def test_the_token_never_appears_in_the_logs(tg, caplog):
    with caplog.at_level(logging.DEBUG):
        alerter(tg)("something broke")
    assert TOKEN not in caplog.text and "FAKE-token" not in caplog.text


@pytest.mark.parametrize("bad", ["abc", "111,abc", "1.5"])
def test_user_ids_that_are_not_numbers_are_a_startup_error_not_silently_ignored(bad):
    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_USERS"):
        alerts.make_alerter("render", {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ALLOWED_USERS": bad})
