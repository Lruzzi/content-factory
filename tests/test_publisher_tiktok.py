"""TikTok, assisted: the approved clip and its caption are sent to your Telegram, and you post it from the TikTok app.
TikTok's own API only posts publicly for audited apps (see docs/TECH.md), so this is the way until then."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import yaml
from fake_telegram_server import fake_telegram

import config_schema as cs
from common import jobs, sources
from common.media import LocalStore
from common.telegram import Telegram
from services.publisher import accounts as acc
from services.publisher import tiktok_assisted as ta

pytestmark = pytest.mark.integration

TOKEN = "123456789:FAKE-token_for_tests"  # noqa: S105  # gitleaks:allow
KEY = "clips/real.mp4"
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def accounts_file(tmp_path: Path, posting: str | None = "assisted", **tt: Any) -> Path:
    defaults = {"daily_cap": 3, "min_spacing_min": 0, "post_windows": [], "visibility": "public"}
    rows = [
        {"id": "yt_test", "platform": "shorts", "test": True},
        {"id": "tt_test", "platform": "tiktok", "test": True, "credentials": {}} | tt,
    ]
    raw: dict[str, Any] = {"timezone": "Asia/Jakarta", "defaults": defaults, "accounts": rows}
    if posting is not None:
        raw["tiktok"] = {"posting": posting}
    path = tmp_path / "accounts.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


# ---------- which mode ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("posting", "expected"), [("assisted", "assisted"), ("api", "api"), (None, "assisted")])
def test_the_mode_is_read_from_the_accounts_file_and_defaults_to_the_safe_one(tmp_path, posting, expected):
    assert acc.tiktok_posting(accounts_file(tmp_path, posting)) == expected


def test_a_mode_that_is_not_one_of_the_two_is_refused(tmp_path):
    with pytest.raises(ValueError, match="assisted"):
        acc.tiktok_posting(accounts_file(tmp_path, "robot"))


def test_the_real_accounts_file_says_assisted():
    assert acc.tiktok_posting(cs.ROOT / "accounts.yaml") == "assisted"


# ---------- delivering a clip ---------------------------------------------------------------------
class World:
    def __init__(self, db, tmp_path, fake, url):
        self.db, self.tmp, self.fake, self.url = db, tmp_path, fake, url
        self.store = LocalStore(tmp_path / "media")
        path = self.store.path(KEY)
        path.parent.mkdir(parents=True)
        path.write_bytes(bytes(range(256)) * 400)  # 100 KiB
        self.alerts: list[str] = []

    def publisher(self, chats=(111,), **kw: Any) -> ta.TikTokAssistedPublisher:
        return ta.TikTokAssistedPublisher(
            self.db, self.store, acc.load(accounts_file(self.tmp)), Telegram(TOKEN, base_url=self.url), list(chats),
            alert=self.alerts.append, **kw,
        )  # fmt: skip

    def job(self, *, targets=("tt_test",)) -> jobs.Job:
        with self.db.begin() as conn:
            sources.seed_from_yaml(conn, cs.ROOT)
            profiles, _ = sources.load_profiles(conn, cs.ROOT)
            profile = next(p for p in profiles if p.source.id == "leokabuki")
            profile.test_targets = [cs.Target(account=t) for t in targets]
            source_id = conn.execute(sa.text("SELECT id FROM sources WHERE source_key = 'leokabuki'")).scalar_one()
            data = {
                "render": {"output": {"key": KEY, "duration_s": 26.7}},
                "package": {"tiktok": {"title": "Judul klip", "text": "Isi klip\n\n#mlbb #fyp", "hashtags": ["#mlbb"]}},
            }
            job_id = jobs.create_job(conn, profile, stage="publish", source_id=source_id, media_key=KEY, data=data)
            return jobs.get(conn, job_id)  # type: ignore[return-value]

    def rows(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(sa.text("SELECT * FROM posts ORDER BY account_id")).mappings()]

    def status(self, job) -> str:
        with self.db.connect() as conn:
            return jobs.get(conn, job["id"])["status"]  # type: ignore[index]


@pytest.fixture
def world(db, tmp_path):
    with fake_telegram() as (fake, url):
        yield World(db, tmp_path, fake, url)


def test_the_clip_goes_to_your_telegram_as_a_file_with_its_caption_as_a_separate_message_to_copy(world):
    job = world.job()
    world.publisher().process(job)
    (doc,) = world.fake.calls_to("sendDocument")
    assert doc.fields["chat_id"] == "111" and doc.files["document"] == world.store.path(KEY).read_bytes()
    assert "tt_test" in doc.fields["caption"] and "Judul klip" in doc.fields["caption"]
    (text,) = world.fake.calls_to("sendMessage")
    assert (
        text.fields["text"] == "Isi klip\n\n#mlbb #fyp"
    )  # exactly the caption, nothing around it to cut off before pasting


def test_the_post_is_recorded_as_done_with_the_telegram_message_as_its_id_and_the_clip_is_finished(world):
    job = world.job()
    world.publisher().process(job)
    (row,) = world.rows()
    assert (row["account_id"], row["state"]) == ("tt_test", "posted") and row["remote_id"].startswith("telegram:")
    assert world.status(job) == "done"


def test_a_clip_is_delivered_once_however_often_it_is_processed(world):
    job = world.job()
    publisher = world.publisher()
    publisher.process(job)
    publisher.process(job)
    assert len(world.fake.calls_to("sendDocument")) == 1 and len(world.rows()) == 1


def test_every_allowed_person_gets_it(world):
    world.publisher(chats=(111, 222)).process(world.job())
    assert sorted(c.fields["chat_id"] for c in world.fake.calls_to("sendDocument")) == ["111", "222"]


def test_only_the_tiktok_accounts_are_touched(world):
    job = world.job(targets=("yt_test", "tt_test"))
    world.publisher().process(job)
    assert [r["account_id"] for r in world.rows()] == ["tt_test"] and world.status(job) == "queued"


def test_a_telegram_failure_is_retried_later_and_only_alerts_when_it_keeps_failing(world):
    job = world.job()
    world.fake.errors["sendDocument"] = (500, "boom")
    publisher = world.publisher(max_attempts=2)
    publisher.process(job)
    assert world.rows()[0]["state"] == "uploading" and world.alerts == []
    world.fake.errors.clear()
    publisher.process(job)
    assert world.rows()[0]["state"] == "posted" and len(world.fake.calls_to("sendDocument")) == 2


def test_a_failure_that_does_not_go_away_ends_the_post_with_an_alert(world):
    job = world.job()
    world.fake.errors["sendDocument"] = (500, "boom")
    publisher = world.publisher(max_attempts=2)
    publisher.process(job)
    publisher.process(job)
    assert world.rows()[0]["state"] == "failed" and len(world.alerts) == 1


def test_the_caption_longer_than_a_telegram_message_is_cut_not_refused(world):
    job = world.job()
    with world.db.begin() as conn:
        conn.execute(
            sa.text("UPDATE jobs SET data = jsonb_set(data, '{package,tiktok,text}', CAST(:t AS jsonb)) WHERE id = :i"),
            {"t": '"' + "x" * 5000 + '"', "i": job["id"]},
        )
        job = jobs.get(conn, job["id"])
    world.publisher().process(job)
    assert len(world.fake.calls_to("sendMessage")[0].fields["text"]) <= 4096


# ---------- a clip too big for a bot --------------------------------------------------------------
def big_clip(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    video = ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25"]
    audio = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
    encode = ["-t", "20", "-c:v", "libx264", "-b:v", "3M", "-minrate", "3M", "-maxrate", "3M", "-bufsize", "6M"]
    subprocess.run(  # noqa: S603  # nosec B603
        [ffmpeg, "-v", "error", "-y", *video, *audio, *encode, "-c:a", "aac", str(path)], check=True, timeout=120
    )
    return path


@needs_ffmpeg
def test_a_clip_over_the_bot_limit_is_sent_as_a_lighter_copy_that_fits(world):
    big_clip(world.store.path(KEY))
    original = world.store.path(KEY).stat().st_size
    limit = original // 3
    world.publisher(max_bytes=limit).process(world.job())
    (doc,) = world.fake.calls_to("sendDocument")
    assert len(doc.files["document"]) <= limit < original
    assert world.store.path(KEY).stat().st_size == original  # the stored clip is untouched


@needs_ffmpeg
def test_a_clip_under_the_limit_is_sent_untouched(world):
    big_clip(world.store.path(KEY))
    world.publisher(max_bytes=10**9).process(world.job())
    assert world.fake.calls_to("sendDocument")[0].files["document"] == world.store.path(KEY).read_bytes()


def test_the_default_limit_leaves_room_under_telegrams_50_mb():
    assert ta.MAX_BYTES < 50 * 1024 * 1024


# ---------- R2: the clip as a link, with a delete button ----------------------------------------------
from fake_s3_server import fake_s3  # noqa: E402

from common.r2 import R2  # noqa: E402
from services.publisher import hosted  # noqa: E402


def media_host_file(tmp_path: Path, **overrides: Any) -> Path:
    section = {
        "provider": "r2", "bucket_env": "R2_BUCKET", "account_id_env": "R2_ACCOUNT_ID",
        "access_key_env": "R2_ACCESS_KEY_ID", "secret_key_env": "R2_SECRET_ACCESS_KEY",
        "link_ttl_min": 2880, "keep_after_approval_h": 24, "max_age_h": 48, "max_total_gb": 8,
    } | overrides  # fmt: skip
    path = tmp_path / "accounts-r2.yaml"
    path.write_text(yaml.safe_dump({"accounts": [], "media_host": section}))
    return path


def test_the_media_host_settings_are_read_with_their_limits(tmp_path):
    host = acc.media_host(media_host_file(tmp_path))
    assert host is not None and (host.link_ttl_min, host.keep_after_approval_h, host.max_age_h, host.max_total_gb) == (
        2880, 24, 48, 8,
    )  # fmt: skip
    assert host.bucket_env == "R2_BUCKET"


def test_no_media_host_section_or_another_provider_means_no_r2(tmp_path):
    assert acc.media_host(accounts_file(tmp_path)) is None
    assert acc.media_host(media_host_file(tmp_path, provider="s3")) is None


def test_the_real_accounts_file_has_a_media_host_with_room_to_spare_under_the_free_tier():
    host = acc.media_host(cs.ROOT / "accounts.yaml")
    assert host is not None and host.max_total_gb <= 8  # R2's free tier is 10 GB
    assert host.link_ttl_min * 60 <= 7 * 86400 and host.link_ttl_min <= host.max_age_h * 60  # no link outlives its file
    assert host.keep_after_approval_h == 24 and host.max_age_h >= host.keep_after_approval_h * 2  # approved: a day


class R2World(World):
    def __init__(self, db, tmp_path, fake, url, s3, s3_url):
        super().__init__(db, tmp_path, fake, url)
        self.s3 = s3
        secret = "sk-test-0123456789"  # noqa: S105  # gitleaks:allow
        self.r2 = R2(account_id="acct", bucket="clips", access_key="ak", secret_key=secret, endpoint=s3_url)
        host = acc.media_host(media_host_file(tmp_path))
        assert host is not None
        self.host = host


@pytest.fixture
def r2world(db, tmp_path):
    with fake_telegram() as (tg, tg_url), fake_s3() as (s3, s3_url):
        yield R2World(db, tmp_path, tg, tg_url, s3, s3_url)


def r2_publisher(w, **kw):
    return ta.TikTokAssistedPublisher(
        w.db, w.store, acc.load(accounts_file(w.tmp)), Telegram(TOKEN, base_url=w.url), [111],
        alert=w.alerts.append, r2=w.r2, host=w.host, **kw,
    )  # fmt: skip


def test_with_r2_the_clip_is_stored_as_it_is_and_you_get_a_link_a_caption_and_a_delete_button(r2world):
    w = r2world
    job = w.job()
    r2_publisher(w, max_bytes=1000).process(job)  # the bot limit is tiny here: nothing is re-encoded for it
    assert w.fake.calls_to("sendDocument") == []
    (stored,) = w.s3.objects.values()
    assert stored == w.store.path(KEY).read_bytes()
    link, caption = w.fake.calls_to("sendMessage")
    assert (
        "Judul klip" in link.fields["text"]
        and "X-Amz-Signature" in link.fields["text"]
        and "24 h after you approved" in link.fields["text"]
    )
    button = link.fields["reply_markup"]["inline_keyboard"][0][0]
    assert button["callback_data"].startswith("x:") and "Delete" in button["text"]
    assert caption.fields["text"] == "Isi klip\n\n#mlbb #fyp"
    with w.db.connect() as conn:
        file = hosted.get(conn, button["callback_data"][2:])
    assert file is not None and file.chats and file.bytes == len(stored)


def test_the_post_is_recorded_once_and_a_second_pass_does_not_upload_or_send_again(r2world):
    w = r2world
    job, publisher = w.job(), r2_publisher(w)
    publisher.process(job)
    publisher.process(job)
    assert len([c for c in w.s3.calls if c[0] == "PUT"]) == 1 and len(w.fake.calls_to("sendMessage")) == 2
    assert w.rows()[0]["state"] == "posted"


def test_over_the_size_cap_it_falls_back_to_sending_the_file_and_stores_nothing(r2world):
    w = r2world
    job = w.job()
    tiny = acc.MediaHost("a", "b", "c", "d", 60, 24, 72, 0.0000001)  # a cap of 100 bytes
    ta.TikTokAssistedPublisher(
        w.db, w.store, acc.load(accounts_file(w.tmp)), Telegram(TOKEN, base_url=w.url), [111],
        alert=w.alerts.append, r2=w.r2, host=tiny,
    ).process(job)  # fmt: skip
    assert w.s3.objects == {} and len(w.fake.calls_to("sendDocument")) == 1


def test_a_failure_to_store_is_retried_and_nothing_is_announced_before_the_file_is_there(r2world):
    w = r2world
    job = w.job()
    w.s3.fail["PUT"] = 500
    publisher = r2_publisher(w)
    publisher.process(job)
    assert w.fake.calls_to("sendMessage") == [] and w.rows()[0]["state"] == "uploading"
    w.s3.fail.clear()
    publisher.process(job)
    assert w.rows()[0]["state"] == "posted" and len(w.fake.calls_to("sendMessage")) == 2


def deliver(w):
    publisher = r2_publisher(w)
    publisher.process(w.job())
    with w.db.connect() as conn:
        (file_id,) = conn.execute(sa.text("SELECT id FROM hosted_files")).scalars()
    return publisher, file_id


def test_when_you_press_delete_the_file_goes_from_r2_and_the_message_says_so(r2world):
    w = r2world
    publisher, file_id = deliver(w)
    with w.db.begin() as conn:
        assert hosted.request_delete(conn, file_id)
    publisher.housekeeping()
    assert w.s3.objects == {}
    edit = w.fake.calls_to("editMessageText")[0].fields
    assert edit["chat_id"] == 111 and "deleted" in edit["text"].lower()
    publisher.housekeeping()  # nothing more to do
    assert len(w.fake.calls_to("editMessageText")) == 1


def test_a_file_older_than_the_limit_is_deleted_by_itself_and_the_message_says_it_expired(r2world):
    w = r2world
    publisher, _ = deliver(w)
    with w.db.begin() as conn:
        conn.execute(sa.text("UPDATE hosted_files SET created_at = clock_timestamp() - interval '80 hours'"))
    publisher.housekeeping()
    assert w.s3.objects == {} and "expired" in w.fake.calls_to("editMessageText")[0].fields["text"].lower()


def test_a_file_that_is_neither_asked_for_nor_old_is_left_alone(r2world):
    w = r2world
    publisher, _ = deliver(w)
    publisher.housekeeping()
    assert len(w.s3.objects) == 1 and w.fake.calls_to("editMessageText") == []


def test_if_the_store_refuses_the_delete_it_is_tried_again_next_time(r2world):
    w = r2world
    publisher, file_id = deliver(w)
    with w.db.begin() as conn:
        hosted.request_delete(conn, file_id)
    w.s3.fail["DELETE"] = 500
    publisher.housekeeping()
    assert len(w.s3.objects) == 1
    w.s3.fail.clear()
    publisher.housekeeping()
    assert w.s3.objects == {}


def test_a_message_that_can_no_longer_be_edited_does_not_stop_the_delete(r2world):
    w = r2world
    publisher, file_id = deliver(w)
    with w.db.begin() as conn:
        hosted.request_delete(conn, file_id)
    w.fake.errors["editMessageText"] = (400, "message to edit not found")
    publisher.housekeeping()
    assert w.s3.objects == {}


# ---------- the approval room ------------------------------------------------------------------------------------
def test_with_a_group_the_clip_and_caption_go_into_the_approval_topic_once(world):
    from common.rooms import Rooms

    publisher = ta.TikTokAssistedPublisher(
        world.db, world.store, acc.load(accounts_file(world.tmp)), Telegram(TOKEN, base_url=world.url), [111, 222],
        alert=world.alerts.append, rooms=Rooms(-1001234567890, {"approval": 11}),
    )  # fmt: skip
    publisher.process(world.job())
    (doc,) = world.fake.calls_to("sendDocument")
    (text,) = world.fake.calls_to("sendMessage")
    assert doc.fields["chat_id"] == "-1001234567890" and doc.fields["message_thread_id"] == "11"
    assert text.fields["chat_id"] == -1001234567890 and text.fields["message_thread_id"] == 11


# ---------- the posting switch -----------------------------------------------------------------------------------
def test_nothing_is_posted_while_posting_is_paused_and_it_goes_when_resumed(world):
    from common import pauses

    job = world.job()
    with world.db.begin() as conn:
        pauses.hold(conn, "switch:publish", "paused from Telegram")
    publisher = world.publisher()
    publisher.process(job)
    assert world.rows() == [] and world.fake.calls_to("sendDocument") == [] and world.status(job) == "queued"
    with world.db.begin() as conn:
        pauses.release(conn, "switch:publish")
    publisher.process(job)
    assert len(world.fake.calls_to("sendDocument")) == 1 and world.status(job) == "done"


# ---------- how Instagram clips reach Instagram (same file, the same media_host) ----------
@pytest.mark.parametrize(
    ("body", "expected"),
    [({}, "resumable"), ({"instagram": {"upload": "r2"}}, "r2"), ({"instagram": {"upload": "resumable"}}, "resumable")],
)
def test_instagram_upload_defaults_to_the_resumable_upload_and_can_be_a_signed_link(tmp_path, body, expected):
    path = tmp_path / "accounts.yaml"
    path.write_text(yaml.safe_dump({"accounts": [], **body}))
    assert acc.instagram_upload(path) == expected


def test_an_unknown_instagram_upload_mode_is_refused(tmp_path):
    path = tmp_path / "accounts.yaml"
    path.write_text(yaml.safe_dump({"accounts": [], "instagram": {"upload": "carrier-pigeon"}}))
    with pytest.raises(ValueError, match="instagram.upload"):
        acc.instagram_upload(path)


def test_the_copy_stored_for_review_is_the_one_tiktok_links_to_and_it_is_not_uploaded_again(r2world):
    from services.publisher.clipstore import ClipStore

    w = r2world
    job = w.job()
    ClipStore(w.db, w.r2, w.host).put(job["id"], w.store.path(KEY), pending_h=24)  # what the review did
    r2_publisher(w).process(job)
    assert len([c for c in w.s3.calls if c[0] == "PUT"]) == 1 and list(w.s3.objects) == [
        f"clips/review/{job['id']}.mp4"
    ]
    link = w.fake.calls_to("sendMessage")[0].fields["text"]
    assert f"review/{job['id']}.mp4" in link and "X-Amz-Signature" in link
