"""Deciding from yt-dlp's answer whether a channel is live (FR-15). The sample answer has the fields a real
`/live` check returned for a public live stream; members-only and private are never recorded."""

from __future__ import annotations

import json
import subprocess

import pytest

from services.live_watcher import detect

LIVE = {
    "id": "Y7iv3AoNJ0s",
    "title": "GAS LIVE MLBB CHARITY",
    "live_status": "is_live",
    "availability": "public",
    "channel_id": "UCf-wG6CXV-ReA-J6RxUniDA",
    "webpage_url": "https://www.youtube.com/watch?v=Y7iv3AoNJ0s",
    "release_timestamp": 1792724319,
}


def test_a_public_live_stream_is_to_be_recorded():
    found = detect.classify(LIVE)
    assert found == detect.Detection(
        "live", "Y7iv3AoNJ0s", "https://www.youtube.com/watch?v=Y7iv3AoNJ0s", "GAS LIVE MLBB CHARITY", ""
    )


def test_an_unlisted_live_stream_is_still_recorded():
    assert detect.classify({**LIVE, "availability": "unlisted"}).state == "live"  # type: ignore[union-attr]


@pytest.mark.parametrize("availability", ["subscriber_only", "premium_only", "needs_auth", "private"])
def test_members_only_private_and_other_restricted_streams_are_skipped_with_a_reason(availability):
    found = detect.classify({**LIVE, "availability": availability})
    assert found is not None and found.state == "skipped" and found.reason == availability


def test_a_missing_availability_is_not_trusted():
    info = {k: v for k, v in LIVE.items() if k != "availability"}
    found = detect.classify(info)
    assert found is not None and found.state == "skipped"


def test_a_scheduled_stream_is_upcoming():
    assert detect.classify({**LIVE, "live_status": "is_upcoming"}).state == "upcoming"  # type: ignore[union-attr]


@pytest.mark.parametrize("status", ["was_live", "not_live", "post_live", None, "something_new"])
def test_anything_else_is_not_a_live_stream(status):
    assert detect.classify({**LIVE, "live_status": status}) is None


@pytest.mark.parametrize(
    "bad", [{"live_status": "is_live"}, {**LIVE, "id": ""}, {**LIVE, "id": "../x"}, {**LIVE, "id": "a b"}]
)
def test_an_answer_without_a_usable_video_id_is_an_error(bad):
    with pytest.raises(detect.DetectError):
        detect.classify(bad)


def test_the_check_asks_for_the_channels_live_page_with_an_argument_list():
    cmd = detect.command("/bin/yt-dlp", "https://www.youtube.com/@leokabuki")
    assert cmd[0] == "/bin/yt-dlp" and "--no-download" in cmd and "--dump-single-json" in cmd
    assert cmd[-1] == "https://www.youtube.com/@leokabuki/live"
    assert detect.command("/bin/yt-dlp", "https://www.youtube.com/@leokabuki/")[-1] == cmd[-1]


@pytest.mark.parametrize(
    "bad",
    [
        "--help",
        "file:///etc/passwd",
        "https://evil.example/@a",
        "http://www.youtube.com/@a",
        "https://www.youtube.com/@a b",
    ],
)
def test_only_https_youtube_urls_are_checked(bad):
    with pytest.raises(ValueError, match="url"):
        detect.command("/bin/yt-dlp", bad)


def fake(returncode=0, out="", err=""):
    return lambda cmd: subprocess.CompletedProcess(cmd, returncode, out, err)


def test_check_returns_what_the_answer_says():
    found = detect.check("https://www.youtube.com/@leokabuki", run=fake(0, json.dumps(LIVE)))
    assert found is not None and found.state == "live" and found.video_id == "Y7iv3AoNJ0s"


def test_a_channel_that_is_not_live_is_none_not_an_error():
    err = "ERROR: [youtube:tab] @leokabuki: The channel is not currently live"
    assert detect.check("https://www.youtube.com/@leokabuki", run=fake(1, "", err)) is None


def test_other_failures_are_errors_that_show_the_reason():
    with pytest.raises(detect.DetectError, match="HTTP Error 429"):
        detect.check("https://www.youtube.com/@leokabuki", run=fake(1, "", "ERROR: HTTP Error 429: Too Many Requests"))


def test_an_answer_that_is_not_json_is_an_error():
    with pytest.raises(detect.DetectError):
        detect.check("https://www.youtube.com/@leokabuki", run=fake(0, "<html>"))


def test_a_video_is_live_when_yt_dlp_says_so():
    assert detect.is_live("Y7iv3AoNJ0s", run=fake(0, json.dumps(LIVE))) is True
    assert detect.is_live("Y7iv3AoNJ0s", run=fake(0, json.dumps({**LIVE, "live_status": "was_live"}))) is False


def test_a_video_that_cannot_be_checked_is_an_error_not_a_guess():
    with pytest.raises(detect.DetectError):
        detect.is_live("Y7iv3AoNJ0s", run=fake(1, "", "ERROR: HTTP Error 429"))


def test_only_a_plain_video_id_is_checked():
    for bad in ["", "a b", "../x", "https://x.example/y"]:
        with pytest.raises(ValueError, match="video id"):
            detect.is_live(bad, run=fake(0, "{}"))


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: [youtube] w-2HNjX4X8o: This live event will begin in a few moments.",
        "ERROR: [youtube] w-2HNjX4X8o: This live event will begin in 2 hours.",
        "ERROR: [youtube] w-2HNjX4X8o: This live event will begin in 14 minutes.",
    ],
)
def test_a_stream_that_is_about_to_start_is_upcoming_not_an_error(message):
    found = detect.check("https://www.youtube.com/@leokabuki", run=fake(1, "", message))
    assert found is not None and found.state == "upcoming" and found.video_id == "w-2HNjX4X8o"


def test_an_upcoming_message_without_a_video_id_is_still_not_an_error():
    assert (
        detect.check(
            "https://www.youtube.com/@a", run=fake(1, "", "ERROR: This live event will begin in a few moments.")
        )
        is None
    )
