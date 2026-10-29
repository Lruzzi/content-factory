"""Reading what yt-dlp says about a channel's streams and uploads. The command is an argument list and the link is
checked first; the parsing is pure."""

from __future__ import annotations

import json

import pytest

from services.discover import youtube as yt

FLAT = {
    "_type": "playlist",
    "entries": [
        {
            "id": "aaaaaaaaaaa",
            "title": "Solo rank Day 1",
            "duration": 7200,
            "view_count": 5000,
            "live_status": "was_live",
        },
        {"id": "bbbbbbbbbbb", "title": "Upcoming", "duration": None, "view_count": None, "live_status": "is_upcoming"},
        {
            "id": "ccccccccccc",
            "title": "Nested",
            "entries": [{"id": "ddddddddddd", "title": "Inner", "duration": 1000}],
        },
        {"title": "no id at all"},
    ],
}
INFO = {
    "id": "aaaaaaaaaaa",
    "title": "Solo rank Day 1",
    "duration": 7200,
    "view_count": 5000,
    "live_status": "was_live",
    "availability": "public",
    "timestamp": 1_761_000_000,
    "heatmap": [
        {"start_time": 0.0, "end_time": 100.0, "value": 0.2},
        {"start_time": 100.0, "end_time": 200.0, "value": 1.0},
    ],
}


def test_the_tabs_of_a_channel_are_its_streams_and_its_videos():
    assert yt.tab_urls("https://www.youtube.com/@name") == [
        "https://www.youtube.com/@name/streams",
        "https://www.youtube.com/@name/videos",
    ]
    assert yt.tab_urls("https://www.youtube.com/@name/live/") == yt.tab_urls("https://www.youtube.com/@name")
    assert yt.tab_urls("https://www.youtube.com/channel/UCabc123/streams")[0].endswith("/channel/UCabc123/streams")


@pytest.mark.parametrize(
    "bad", ["--help", "file:///etc/passwd", "https://evil.example/@x", "https://www.youtube.com/@x y", ""]
)
def test_a_link_that_is_not_a_youtube_channel_is_refused_before_anything_runs(bad):
    with pytest.raises(ValueError, match="channel"):
        yt.tab_urls(bad)


def test_the_commands_are_argument_lists_that_never_download_the_video():
    flat = yt.flat_command("/bin/yt-dlp", "https://www.youtube.com/@name/videos", 30)
    assert flat[0] == "/bin/yt-dlp" and "--flat-playlist" in flat and "--download-sections" not in flat
    assert flat[flat.index("--playlist-end") + 1] == "30" and flat[-1].endswith("/@name/videos")
    one = yt.info_command("/bin/yt-dlp", "aaaaaaaaaaa")
    assert "--no-download" in one and one[-1] == "https://www.youtube.com/watch?v=aaaaaaaaaaa"


@pytest.mark.parametrize("bad", ["--help", "a b", "../x", ""])
def test_a_video_id_that_is_not_one_is_refused(bad):
    with pytest.raises(ValueError, match="video id"):
        yt.info_command("/bin/yt-dlp", bad)


def test_the_channels_list_becomes_videos_with_the_nested_ones_flattened_and_id_less_ones_dropped():
    videos = yt.parse_flat(FLAT)
    assert [v.id for v in videos] == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ddddddddddd"]
    first = videos[0]
    assert (first.title, first.duration_s, first.views, first.live_status) == (
        "Solo rank Day 1",
        7200.0,
        5000,
        "was_live",
    )
    assert videos[1].duration_s is None and videos[1].views == 0


def test_a_videos_own_page_gives_availability_upload_time_and_the_heatmap():
    v = yt.parse_info(INFO)
    assert v.availability == "public" and v.uploaded is not None and v.uploaded.year == 2025
    assert v.heatmap == ((0.0, 100.0, 0.2), (100.0, 200.0, 1.0))


def test_an_upload_date_alone_is_enough_and_a_missing_heatmap_is_an_empty_one():
    v = yt.parse_info({"id": "aaaaaaaaaaa", "title": "t", "upload_date": "20251020"})
    assert v.uploaded is not None and (v.uploaded.month, v.uploaded.day) == (10, 20) and v.heatmap == ()


def test_listing_asks_each_tab_and_one_tab_that_does_not_exist_does_not_stop_the_other():
    asked: list[str] = []

    def run(command: list[str]) -> str:
        asked.append(command[-1])
        if command[-1].endswith("/streams"):
            raise yt.DiscoverError("this channel does not have a streams tab")
        return json.dumps(FLAT)

    videos = yt.list_videos("https://www.youtube.com/@name", run=run)
    assert asked == ["https://www.youtube.com/@name/streams", "https://www.youtube.com/@name/videos"]
    assert [v.id for v in videos] == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ddddddddddd"]


def test_when_every_tab_fails_that_is_an_error():
    def run(command: list[str]) -> str:
        raise yt.DiscoverError("boom")

    with pytest.raises(yt.DiscoverError, match="boom"):
        yt.list_videos("https://www.youtube.com/@name", run=run)


def test_the_same_video_in_both_tabs_is_listed_once():
    def run(command: list[str]) -> str:
        return json.dumps(FLAT)

    ids = [v.id for v in yt.list_videos("https://www.youtube.com/@name", run=run)]
    assert len(ids) == len(set(ids))


def test_a_reply_that_is_not_json_is_an_error_not_a_crash():
    with pytest.raises(yt.DiscoverError, match="JSON"):
        yt.video_info("aaaaaaaaaaa", run=lambda command: "<html>")
