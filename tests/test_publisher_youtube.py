"""Uploading a clip to YouTube with the resumable upload protocol, and checking what YouTube did with it (FR-9)."""

from __future__ import annotations

import logging

import pytest
from fake_google_server import fake_google

from services.publisher import google_oauth as g
from services.publisher import youtube as yt

SECRET_TOKEN = "access-token-for-tests"  # noqa: S105  # gitleaks:allow
CHUNK = 256 * 1024


@pytest.fixture
def api(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(bytes(range(256)) * 3000)  # 750 KiB: three chunks
    with fake_google() as (fake, url):
        yield fake, yt.YouTube(lambda: SECRET_TOKEN, base_url=url, chunk_bytes=CHUNK, retry_wait_s=0), video


def up(client, video, **kw):
    options = {"title": "Judul", "description": "Isi klip", "privacy": "private"} | kw
    return client.upload(video, **options)


def test_the_clip_is_uploaded_whole_in_chunks_and_the_new_video_id_comes_back(api):
    fake, client, video = api
    result = up(client, video)
    assert result.video_id == "vid123" and result.url == "https://www.youtube.com/watch?v=vid123"
    assert bytes(fake.uploads["s1"]) == video.read_bytes()
    assert len([c for c in fake.calls if c.method == "PUT"]) == 3


def test_the_title_description_and_privacy_asked_for_are_what_is_sent(api):
    fake, client, video = api
    up(client, video, title="Donasi heboh", description="Streamer kaget", privacy="unlisted")
    sent = fake.metadata[0]
    assert sent["snippet"]["title"] == "Donasi heboh" and sent["snippet"]["description"] == "Streamer kaget"
    assert sent["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}
    start = fake.calls_to("/upload/youtube/v3/videos")[0]
    assert start.query == {"uploadType": "resumable", "part": "snippet,status"}
    assert start.headers["Authorization"] == f"Bearer {SECRET_TOKEN}"
    assert start.headers["X-Upload-Content-Length"] == str(video.stat().st_size)
    assert start.headers["X-Upload-Content-Type"] == "video/mp4"


def test_a_privacy_that_youtube_does_not_have_is_refused_before_anything_is_sent(api):
    fake, client, video = api
    with pytest.raises(ValueError, match="privacy"):
        up(client, video, privacy="friends")
    assert fake.calls == []


def test_characters_youtube_rejects_and_overlong_titles_are_fixed_before_sending(api):
    fake, client, video = api
    up(client, video, title="<b>judul</b> " + "x" * 200, description="a > b\x00 " + chr(0x202E))
    sent = fake.metadata[0]["snippet"]
    assert "<" not in sent["title"] and ">" not in sent["title"] and len(sent["title"]) <= 100
    assert (
        ">" not in sent["description"] and "\x00" not in sent["description"] and chr(0x202E) not in sent["description"]
    )


def test_a_chunk_that_fails_for_a_moment_is_resumed_from_where_youtube_got_to(api):
    fake, client, video = api
    fake.put_failures = 1  # the first chunk fails once
    up(client, video)
    assert bytes(fake.uploads["s1"]) == video.read_bytes()  # nothing missing and nothing sent twice
    assert any(c.headers["Content-Range"].startswith("bytes */") for c in fake.calls if c.method == "PUT")


def test_after_too_many_failures_the_upload_gives_up_and_says_it_may_be_tried_again(api):
    fake, client, video = api
    fake.put_failures = 99
    with pytest.raises(yt.YouTubeError) as caught:
        up(client, video)
    assert caught.value.retryable is True


def test_the_daily_quota_running_out_is_a_clear_error_that_is_not_retried_in_minutes(api):
    fake, client, video = api
    fake.init_error = (403, "quotaExceeded")
    with pytest.raises(yt.YouTubeError, match="quotaExceeded") as caught:
        up(client, video)
    assert caught.value.retryable is False


def test_a_refused_access_token_means_sign_in_again(api):
    fake, client, video = api
    fake.init_error = (401, "authError")
    with pytest.raises(g.AuthExpired, match="authorize"):
        up(client, video)


def test_a_missing_or_empty_file_is_an_error_and_nothing_is_started(api, tmp_path):
    fake, client, _ = api
    with pytest.raises(yt.YouTubeError, match="file"):
        up(client, tmp_path / "gone.mp4")
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(yt.YouTubeError, match="empty"):
        up(client, empty)
    assert fake.calls == []


def test_what_youtube_did_with_the_video_can_be_read_back(api):
    fake, client, video = api
    result = up(client, video, privacy="public")
    fake.privacy_given = "private"  # an unaudited project: YouTube locks the video to private
    status = client.status(result.video_id)
    assert status == {"privacy": "private", "upload": "processed"}


def test_a_video_that_is_not_found_reads_back_as_none(api):
    _, client, _ = api
    assert client.status("nope") is None


def test_the_access_token_never_appears_in_logs(api, caplog):
    _, client, video = api
    with caplog.at_level(logging.DEBUG):
        up(client, video)
    assert SECRET_TOKEN not in caplog.text


def test_the_channel_the_sign_in_belongs_to_can_be_read(api):
    _, client, _ = api
    assert client.channel() == {"id": "UCtest", "title": "Test Channel"}


# ---------- resuming after a crash --------------------------------------------------------------------------------
def test_the_upload_session_is_reported_before_any_bytes_are_sent_so_it_can_be_saved(api):
    fake, client, video = api
    seen: list[tuple[str, int]] = []
    client.upload(
        video, title="t", description="d", privacy="private",
        on_session=lambda url: seen.append((url, fake.data_puts)),
    )  # fmt: skip
    assert len(seen) == 1 and seen[0][0].startswith(fake.base) and seen[0][1] == 0


def test_an_upload_that_died_halfway_carries_on_from_its_saved_session_without_resending(api):
    fake, client, video = api
    sessions: list[str] = []
    fake.fail_puts_from = 1  # the first chunk goes through, then YouTube keeps failing
    with pytest.raises(yt.YouTubeError):
        up(client, video, on_session=sessions.append)
    assert len(fake.uploads["s1"]) == CHUNK
    fake.fail_puts_from = None
    result = up(client, video, session_url=sessions[0])  # a new run, a new client: only the saved address is known
    assert result.video_id == "vid123" and bytes(fake.uploads["s1"]) == video.read_bytes()
    assert len(fake.metadata) == 1  # no second upload was started


def test_an_upload_that_had_already_finished_is_recognised_and_not_sent_again(api):
    fake, client, video = api
    sessions: list[str] = []
    first = up(client, video, on_session=sessions.append)
    puts_before = fake.data_puts
    again = up(client, video, session_url=sessions[0])
    assert again.video_id == first.video_id and fake.data_puts == puts_before


def test_a_session_youtube_no_longer_knows_cannot_say_whether_the_video_was_posted(api):
    fake, client, video = api
    with pytest.raises(yt.SessionGone) as caught:
        up(client, video, session_url=f"{fake.base}/upload/session/s99")
    assert caught.value.retryable is False


def test_an_upload_address_on_another_host_is_not_followed(api):
    _, client, video = api
    with pytest.raises(yt.YouTubeError, match="another host"):
        up(client, video, session_url="https://evil.example/upload/session/s1")
