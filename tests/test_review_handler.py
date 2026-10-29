"""The review stage as a worker handler: send the clip to Telegram and wait for your tap, or skip it when the profile
does not require approval (FR-8)."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import pytest
import sqlalchemy as sa
from fake_telegram_server import fake_telegram

import config_schema as cs
from common import jobs, sources
from common.media import LocalStore
from common.telegram import Telegram, TelegramError
from services.review import handler as review
from services.review import preview

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")]

TOKEN = "123456789:FAKE-token_for_tests"  # noqa: S105  # gitleaks:allow
KEY = "clips/real.mp4"
TITLE = "Judul klip" + chr(0x202E)  # ends with a right-to-left override, as hostile transcript text might


@pytest.fixture(scope="module")
def clip_bytes(tmp_path_factory):
    """A real 1080x1920 clip, as render makes them."""
    path = tmp_path_factory.mktemp("clip") / "clip.mp4"
    subprocess.run(  # noqa: S603  # nosec B603
        [shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6", "-c:v", "libx264", "-crf", "20",
         "-c:a", "aac", str(path)],
        check=True,
    )  # fmt: skip
    return path.read_bytes()


@pytest.fixture
def world(db, tmp_path, clip_bytes):
    store = LocalStore(tmp_path / "media")
    store.put_bytes(KEY, clip_bytes)
    with fake_telegram() as (fake, url):
        yield store, fake, Telegram(TOKEN, base_url=url)


def make_job(db, *, required=True, key=KEY, title=TITLE):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        profile = next(p for p in profiles if p.source.id == "leokabuki")
        profile.review["required"] = required
        data: dict[str, Any] = {
            "clip": {
                "kind": "donation",
                "score": 0.82,
                "reason": "Streamer bereaksi kocak pada donasi.",
                "llm": "openai_compatible/gemini-3.5-flash",
                "stt": "groq_api/whisper-large-v3-turbo",
            },
            "render": {"hook": "Reaksi Kocak"},
            "package": {"shorts": {"title": title, "text": "t", "hashtags": []}},
        }
        job_id = jobs.create_job(conn, profile, stage="review", kind="donation", media_key=key, data=data)
        return jobs.get(conn, job_id)


def handle(db, world, job, allowed=(111,)):
    store, _, bot = world
    return review.make_handler(db, store, bot, list(allowed))(job)


def stored(db, job):
    with db.connect() as conn:
        return conn.execute(sa.text("SELECT data->'review' FROM jobs WHERE id = :i"), {"i": job["id"]}).scalar_one()


# ---------- the preview file --------------------
def test_the_preview_is_a_small_vertical_copy_of_the_clip(tmp_path, clip_bytes):
    source = tmp_path / "clip.mp4"
    source.write_bytes(clip_bytes)
    out = preview.make_preview(source, tmp_path / "preview.mp4")
    probe = subprocess.run(  # noqa: S603  # nosec B603
        [shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert probe.stdout.strip() == "720,1280" and 0 < out.stat().st_size < source.stat().st_size / 2


@pytest.mark.parametrize(("seconds", "expected_kbps"), [(30, 3500), (60, 3500), (120, 2602), (300, 1002)])
def test_the_longer_the_clip_the_lower_the_preview_bitrate_so_it_stays_under_telegrams_limit(seconds, expected_kbps):
    kbps = preview.video_cap_kbps(seconds)
    assert kbps == expected_kbps
    assert (kbps + preview.AUDIO_KBPS) * seconds / 8 / 1000 < 45  # MB, with room under the bot upload limit of 50


def test_a_file_that_is_not_a_video_is_a_clear_error(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"nope")
    with pytest.raises(preview.PreviewError):
        preview.make_preview(bad, tmp_path / "out.mp4")


# ---------- sending the review --------------------
def test_the_clip_is_sent_to_you_with_what_you_need_to_decide_and_the_job_waits(db, world):
    job = make_job(db)
    assert handle(db, world, job) == jobs.WAIT
    (call,) = world[1].calls_to("sendVideo")
    caption = call.fields["caption"]
    assert call.fields["chat_id"] == "111" and "parse_mode" not in call.fields
    assert (
        "Judul klip" in caption and "donation" in caption and "0.82" in caption and "Streamer bereaksi kocak" in caption
    )
    assert "yt_test" in caption and "rnd" in caption  # where it would go, and in which mode
    assert "gemini-3.5-flash" in caption and "whisper-large-v3-turbo" in caption  # who chose it and who transcribed it
    assert chr(0x202E) not in caption  # a right-to-left override from the transcript is stripped
    buttons = json.loads(call.fields["reply_markup"])["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == [f"a:{job['id']}", f"r:{job['id']}"]
    assert len(call.files["video"]) < len(world[0].path(KEY).read_bytes())  # the preview, not the full clip


def test_where_the_messages_are_is_stored_so_they_can_be_updated_later(db, world):
    job = make_job(db)
    handle(db, world, job)
    review_data = stored(db, job)
    assert review_data["state"] == "pending" and review_data["title"] == "Judul klip"
    assert review_data["chats"] == [{"chat_id": 111, "message_id": world[1].next_message_id}]


def test_every_allowed_user_gets_the_clip(db, world):
    job = make_job(db)
    handle(db, world, job, allowed=(111, 222))
    assert [c.fields["chat_id"] for c in world[1].calls_to("sendVideo")] == ["111", "222"]
    assert len(stored(db, job)["chats"]) == 2


def test_a_job_delivered_again_while_it_waits_is_not_sent_twice(db, world):
    job = make_job(db)
    handle(db, world, job)
    with db.connect() as conn:
        again = jobs.get(conn, job["id"])
    assert handle(db, world, again) == jobs.WAIT and len(world[1].calls_to("sendVideo")) == 1


def test_when_telegram_refuses_everyone_nothing_is_stored_and_the_error_reaches_the_worker(db, world):
    world[1].errors["sendVideo"] = (403, "Forbidden: bot was blocked by the user")
    job = make_job(db)
    with pytest.raises(TelegramError, match="blocked"):
        handle(db, world, job)
    assert stored(db, job) is None


def test_if_only_some_users_can_be_reached_the_clip_still_waits_for_those(db, world):
    job = make_job(db)
    store, fake, bot = world
    original = bot.send_video

    def flaky(chat_id, *args, **kw):
        if chat_id == 222:
            raise TelegramError("sendVideo: Forbidden")
        return original(chat_id, *args, **kw)

    bot.send_video = flaky
    assert review.make_handler(db, store, bot, [111, 222])(job) == jobs.WAIT
    assert [c["chat_id"] for c in stored(db, job)["chats"]] == [111]


def test_a_clip_missing_from_the_store_is_an_error(db, world):
    with pytest.raises(review.ReviewError, match="not in the media store"):
        handle(db, world, make_job(db, key="clips/gone.mp4"))


# ---------- the approval switch --------------------
def test_a_profile_that_does_not_need_approval_goes_straight_on_without_telegram(db, world):
    job = make_job(db, required=False)
    assert handle(db, world, job) == "publish"
    assert world[1].calls == [] and stored(db, job) == {"state": "auto"}


# ---------- the approval room ------------------------------------------------------------------------------------
def test_with_a_group_the_preview_goes_once_into_the_approval_topic_and_is_remembered_there(db, world):
    from common.rooms import Rooms

    store, fake, bot = world
    job = make_job(db)
    rooms = Rooms(-1001234567890, {"approval": 11, "alerts": 12})
    assert review.make_handler(db, store, bot, [111, 222], rooms=rooms)(job) == jobs.WAIT
    (call,) = fake.calls_to("sendVideo")  # once, not once per allowed user
    assert call.fields["chat_id"] == "-1001234567890" and call.fields["message_thread_id"] == "11"
    chats = stored(db, job)["chats"]
    assert [c["chat_id"] for c in chats] == [-1001234567890]


def test_without_a_group_each_allowed_user_still_gets_the_preview(db, world):
    store, fake, bot = world
    review.make_handler(db, store, bot, [111, 222])(make_job(db))
    assert sorted(c.fields["chat_id"] for c in fake.calls_to("sendVideo")) == ["111", "222"]
    assert all("message_thread_id" not in c.fields for c in fake.calls_to("sendVideo"))


# ---------- the full quality copy in R2, sent with the approval ----------
from fake_s3_server import fake_s3  # noqa: E402

from common.r2 import R2  # noqa: E402
from services.publisher import accounts as acc  # noqa: E402
from services.publisher.clipstore import ClipStore  # noqa: E402

HOST = acc.MediaHost(
    bucket_env="B", account_id_env="A", access_key_env="K", secret_key_env="S",  # noqa: S106
    link_ttl_min=2880, keep_after_approval_h=24, max_age_h=48, max_total_gb=8,
)  # fmt: skip


@pytest.fixture
def r2(db):
    with fake_s3() as (s3, url):
        secret = "secret-for-tests"  # noqa: S105
        yield (
            s3,
            ClipStore(db, R2(account_id="a", bucket="clips", access_key="ak", secret_key=secret, endpoint=url), HOST),
        )


def with_clips(db, world, job, clips):
    store, _, bot = world
    return review.make_handler(db, store, bot, [111], clips=clips)(job)


def sent_keyboard(world):
    return json.loads(world[1].calls_to("sendVideo")[0].fields["reply_markup"])["inline_keyboard"]


def test_the_clip_is_stored_in_r2_first_and_the_approval_carries_a_link_to_it(db, world, r2):
    s3, clips = r2
    job = make_job(db)
    with_clips(db, world, job, clips)
    assert (
        s3.objects[f"clips/review/{job['id']}.mp4"] == world[0].path(KEY).read_bytes()
    )  # the full file, not the preview
    keyboard = sent_keyboard(world)
    assert [b.get("callback_data") for b in keyboard[0]] == [f"a:{job['id']}", f"r:{job['id']}"]
    assert keyboard[1][0]["text"].startswith("⬇") and "X-Amz-Signature" in keyboard[1][0]["url"]


def test_a_store_that_is_down_does_not_hold_back_the_approval(db, world, r2):
    s3, clips = r2
    s3.fail["PUT"] = 503
    with_clips(db, world, make_job(db), clips)
    keyboard = sent_keyboard(world)
    assert len(keyboard) == 1 and all("url" not in b for b in keyboard[0])  # Approve and Reject, no link


def test_a_clip_that_needs_no_approval_is_not_stored_for_review(db, world, r2):
    s3, clips = r2
    with_clips(db, world, make_job(db, required=False), clips)
    assert s3.objects == {} and world[1].calls_to("sendVideo") == []


def test_without_a_store_the_approval_is_as_before(db, world):
    handle(db, world, make_job(db))
    assert len(sent_keyboard(world)) == 1
