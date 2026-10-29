"""The Meta Graph client for Facebook Page Reels and Instagram Reels: what it sends, how it reads answers, and that the
Page token never ends up in a URL, an error or a log (FR-9)."""

from __future__ import annotations

import logging

import pytest
from fake_meta_server import V, fake_meta

from services.publisher import meta

TOKEN = "EAAFAKE-page-token-for-tests"  # noqa: S105  # gitleaks:allow


@pytest.fixture
def api(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(bytes(range(256)) * 400)  # 100 KiB
    with fake_meta() as (fake, url):
        yield fake, meta.Meta(TOKEN, graph_url=url, rupload_url=url, retry_wait_s=0), video


# ---------- Facebook Page Reels --------------------
def test_a_reel_is_started_uploaded_and_finished_with_the_token_in_a_header_never_in_the_address(api):
    fake, client, video = api
    video_id = client.fb_reel_start("PAGE1")
    client.fb_upload(video_id, video, offset=0)
    client.fb_reel_finish("PAGE1", video_id, description="Isi #mlbb", title="Judul")
    assert bytes(fake.fb_received[video_id]) == video.read_bytes()
    assert all(f"OAuth {TOKEN}" == c.headers["Authorization"] for c in fake.calls)
    assert all(TOKEN not in c.path and TOKEN not in str(c.query) for c in fake.calls)  # not in any address
    upload = fake.calls_to("POST", rf"/video-upload/{V}/\w+")[0]
    assert upload.headers["offset"] == "0" and upload.headers["file_size"] == str(video.stat().st_size)


def test_the_finish_step_asks_for_publishing_and_keeps_hashtags_intact(api):
    fake, client, video = api
    video_id = client.fb_reel_start("PAGE1")
    client.fb_reel_finish("PAGE1", video_id, description="Isi klip\n\n#mlbb #seru", title="Judul")
    sent = fake.fb_finish[0]
    assert sent["upload_phase"] == "finish" and sent["video_state"] == "PUBLISHED" and sent["video_id"] == video_id
    assert sent["description"] == "Isi klip\n\n#mlbb #seru" and sent["title"] == "Judul"  # a '#' is not cut off


def test_an_upload_carries_on_from_the_offset_facebook_reports(api):
    fake, client, video = api
    video_id = client.fb_reel_start("PAGE1")
    fake.fb_received[video_id].extend(video.read_bytes()[:40_000])  # 40,000 bytes already there
    fake.file_size = video.stat().st_size
    status = client.fb_status(video_id)
    assert status.bytes_transferred == 40_000 and not status.upload_complete
    client.fb_upload(video_id, video, offset=status.bytes_transferred)
    assert bytes(fake.fb_received[video_id]) == video.read_bytes()
    assert client.fb_status(video_id).upload_complete


def test_the_status_says_whether_the_reel_is_published(api):
    _, client, video = api
    video_id = client.fb_reel_start("PAGE1")
    assert client.fb_status(video_id).published is False
    client.fb_upload(video_id, video, offset=0)
    client.fb_reel_finish("PAGE1", video_id, description="d", title="t")
    assert client.fb_status(video_id).published is True


def test_the_permalink_of_a_reel_is_read_back_as_a_full_address(api):
    _, client, _ = api
    video_id = client.fb_reel_start("PAGE1")
    assert client.fb_permalink(video_id) == f"https://www.facebook.com/reel/{video_id}"


# ---------- Instagram Reels --------------------
def test_an_instagram_container_is_created_for_a_resumable_reel_upload(api):
    fake, client, video = api
    container, uri = client.ig_create_container("IG1", caption="Isi #mlbb")
    assert container == "igc1" and uri.startswith(fake.base)
    sent = fake.ig_created[0]
    assert sent["media_type"] == "REELS" and sent["upload_type"] == "resumable" and sent["caption"] == "Isi #mlbb"
    client.ig_upload(uri, video)
    assert bytes(fake.ig_received[container]) == video.read_bytes()


def test_an_instagram_upload_address_on_another_host_is_not_followed(api):
    _, client, video = api
    with pytest.raises(meta.MetaError, match="another host"):
        client.ig_upload("https://evil.example/ig-api-upload/v26.0/igc1", video)


def test_the_container_status_the_publish_and_the_permalink(api):
    fake, client, video = api
    fake.polls_until_finished = 1
    container, uri = client.ig_create_container("IG1", caption="c")
    client.ig_upload(uri, video)
    assert client.ig_status(container) == "IN_PROGRESS"
    assert client.ig_status(container) == "FINISHED"
    media = client.ig_publish("IG1", container)
    assert media == "igm1" and client.ig_permalink(media) == "https://www.instagram.com/reel/igm1/"
    assert client.ig_status(container) == "PUBLISHED"


def test_recent_media_lists_what_the_account_posted(api):
    fake, client, video = api
    container, uri = client.ig_create_container("IG1", caption="c")
    client.ig_upload(uri, video)
    client.ig_status(container)
    client.ig_publish("IG1", container)
    assert [m["id"] for m in client.ig_recent_media("IG1")] == ["igm1"]


# ---------- errors --------------------
def error(code, message="boom", sub=None):
    return {"error": {"message": message, "code": code, "error_subcode": sub, "fbtrace_id": "x"}}


def test_a_token_that_no_longer_works_is_its_own_error(api):
    fake, client, _ = api
    fake.errors["POST /v26.0/PAGE1/video_reels"] = (
        400,
        error(190, "Error validating access token: Session has expired"),
    )
    with pytest.raises(meta.MetaTokenError, match="expired") as caught:
        client.fb_reel_start("PAGE1")
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("code", [4, 17, 32, 368, 613])
def test_a_rate_limit_is_its_own_error_and_is_not_retried_at_once(api, code):
    fake, client, _ = api
    fake.errors["POST /v26.0/PAGE1/video_reels"] = (400, error(code, "we limit how often you can post"))
    with pytest.raises(meta.MetaLimit) as caught:
        client.fb_reel_start("PAGE1")
    assert caught.value.retryable is False and caught.value.code == code


@pytest.mark.parametrize(
    "status,code,retryable", [(500, 2, True), (400, 341, True), (400, 100, False), (403, 200, False)]
)
def test_temporary_problems_may_be_retried_and_the_rest_may_not(api, status, code, retryable):
    fake, client, _ = api
    fake.errors["POST /v26.0/PAGE1/video_reels"] = (status, error(code, "something happened"))
    with pytest.raises(meta.MetaError, match="something happened") as caught:
        client.fb_reel_start("PAGE1")
    assert caught.value.retryable is retryable and not isinstance(caught.value, meta.MetaTokenError)


def test_a_refusal_in_another_shape_than_graph_errors_still_says_why(api):
    fake, client, _ = api
    fake.errors["POST /v26.0/PAGE1/video_reels"] = (
        400,
        {"debug_info": {"retriable": False, "type": "ProcessingFailedError", "message": "Video file is too large"}},
    )
    with pytest.raises(meta.MetaError, match="Video file is too large") as caught:
        client.fb_reel_start("PAGE1")
    assert "HTTP 400" in str(caught.value) and caught.value.retryable is False


def test_an_unknown_refusal_shows_a_short_piece_of_what_was_sent_back_without_the_token(api):
    fake, client, _ = api
    fake.errors["POST /v26.0/PAGE1/video_reels"] = (400, {"odd": "x" * 500, "via": TOKEN})
    with pytest.raises(meta.MetaError) as caught:
        client.fb_reel_start("PAGE1")
    text = str(caught.value)
    assert "odd" in text and len(text) < 400 and TOKEN not in text


def test_a_server_that_cannot_be_reached_is_a_retryable_error_without_the_address(tmp_path):
    client = meta.Meta(
        TOKEN, graph_url="http://127.0.0.1:9", rupload_url="http://127.0.0.1:9", timeout_s=2, retry_wait_s=0
    )
    with pytest.raises(meta.MetaError) as caught:
        client.fb_reel_start("PAGE1")
    assert caught.value.retryable and TOKEN not in str(caught.value) and caught.value.__cause__ is None


def test_the_token_never_appears_in_the_logs(api, caplog):
    fake, client, video = api
    with caplog.at_level(logging.DEBUG):
        video_id = client.fb_reel_start("PAGE1")
        client.fb_upload(video_id, video, offset=0)
        fake.errors["POST /v26.0/PAGE1/video_reels"] = (400, error(100, "bad"))
        with pytest.raises(meta.MetaError):
            client.fb_reel_finish("PAGE1", video_id, description="d", title="t")
    assert TOKEN not in caplog.text and "FAKE-page-token" not in caplog.text


@pytest.mark.parametrize("bad", ["", "has space", "../x", "a/b"])
def test_an_id_that_could_change_the_request_path_is_refused(api, bad):
    _, client, _ = api
    with pytest.raises(ValueError, match="id"):
        client.fb_reel_start(bad)


def test_an_instagram_container_can_be_made_from_a_link_instead_of_an_upload(api):
    fake, client, _ = api
    container = client.ig_create_container_from_url(
        "IG1", caption="hi #x", video_url="https://files.example/c.mp4?sig=1"
    )
    assert container == "igc1"
    form = fake.ig_created[0]
    assert form["video_url"] == "https://files.example/c.mp4?sig=1" and form["media_type"] == "REELS"
    assert form["share_to_feed"] == "true" and "upload_type" not in form


@pytest.mark.parametrize("bad", ["http://files.example/c.mp4", "ftp://x/c.mp4", "file:///etc/passwd", "c.mp4", ""])
def test_a_link_that_is_not_https_is_refused_before_anything_is_sent(api, bad):
    fake, client, _ = api
    with pytest.raises(ValueError, match="link"):
        client.ig_create_container_from_url("IG1", caption="hi", video_url=bad)
    assert fake.calls == []


def test_a_local_http_link_is_allowed_for_tests_only_on_this_machine(api):
    fake, client, _ = api
    client.ig_create_container_from_url("IG1", caption="hi", video_url="http://127.0.0.1:9/c.mp4")
    assert fake.ig_created[0]["video_url"] == "http://127.0.0.1:9/c.mp4"
