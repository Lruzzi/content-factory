"""Approve, reject, expire: the only place a clip becomes approved, and only for the allowed Telegram ids (FR-8)."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from fake_telegram_server import fake_telegram

import config_schema as cs
from common import jobs, sources
from common.telegram import Telegram
from services.review import decide
from services.review.listener import handle_update

pytestmark = pytest.mark.integration

ME, STRANGER = 111, 222
TOKEN = "123456789:FAKE-token_for_tests"  # noqa: S105  # gitleaks:allow


def waiting_job(db, *, age_h=0.0, expire_after_h=24, stage="review", status="waiting"):
    review = {"state": "pending", "title": "Judul klip", "chats": [{"chat_id": ME, "message_id": 55}]}
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        profile = next(p for p in profiles if p.source.id == "leokabuki")
        profile.review["expire_after_h"] = expire_after_h
        job_id = jobs.create_job(conn, profile, stage=stage, kind="donation", data={"review": review, "package": {}})
        conn.execute(
            sa.text(
                "UPDATE jobs SET status = :s, "
                "updated_at = clock_timestamp() - make_interval(secs => :age) WHERE id = :i"
            ),
            {"s": status, "age": age_h * 3600, "i": job_id},
        )
        return job_id


def row(db, job_id):
    with db.connect() as conn:
        return jobs.get(conn, job_id)


# ---------- the decision itself -------------------------------------------------------------------------------
def test_approving_sends_the_clip_on_to_publishing_and_records_who_approved(db):
    job_id = waiting_job(db)
    assert decide.decide(db, job_id, "approved", by=ME) is True
    job = row(db, job_id)
    assert (job["stage"], job["status"], job["attempt"]) == ("publish", "queued", 0)
    assert job["data"]["review"]["state"] == "approved" and job["data"]["review"]["by"] == ME
    assert job["data"]["review"]["title"] == "Judul klip" and "package" in job["data"]  # nothing else is lost


def test_rejecting_finishes_the_job_without_publishing(db):
    job_id = waiting_job(db)
    assert decide.decide(db, job_id, "rejected", by=ME) is True
    job = row(db, job_id)
    assert (job["stage"], job["status"]) == ("review", "done") and job["data"]["review"]["state"] == "rejected"


def test_only_the_first_decision_counts(db):
    job_id = waiting_job(db)
    assert decide.decide(db, job_id, "approved", by=ME) is True
    assert decide.decide(db, job_id, "rejected", by=ME) is False
    assert row(db, job_id)["data"]["review"]["state"] == "approved"


@pytest.mark.parametrize(
    "stage,status", [("review", "queued"), ("publish", "waiting"), ("review", "done"), ("render", "queued")]
)
def test_a_job_that_is_not_waiting_for_review_cannot_be_decided(db, stage, status):
    job_id = waiting_job(db, stage=stage, status=status)
    assert decide.decide(db, job_id, "approved", by=ME) is False
    assert (row(db, job_id)["stage"], row(db, job_id)["status"]) == (stage, status)


def test_an_unknown_decision_is_refused(db):
    with pytest.raises(ValueError):
        decide.decide(db, waiting_job(db), "maybe", by=ME)


# ---------- expiry --------------------------------------------------------------------------------------------------
def test_a_clip_nobody_decided_on_expires_after_the_hours_its_profile_sets(db):
    old, fresh = waiting_job(db, age_h=25), waiting_job(db, age_h=2)
    expired = decide.expire_due(db)
    assert [e.job_id for e in expired] == [old] and expired[0].chats == [{"chat_id": ME, "message_id": 55}]
    assert row(db, old)["status"] == "done" and row(db, old)["data"]["review"]["state"] == "expired"
    assert row(db, fresh)["status"] == "waiting"
    assert decide.expire_due(db) == []  # nothing is expired twice


def test_the_expiry_hours_come_from_each_jobs_own_profile_snapshot(db):
    short, normal = waiting_job(db, age_h=3, expire_after_h=2), waiting_job(db, age_h=3, expire_after_h=24)
    assert [e.job_id for e in decide.expire_due(db)] == [short]
    assert row(db, normal)["status"] == "waiting"


# ---------- taps from Telegram --------------------------------------------------------------------
def tap(data, user=ME, callback_id="cb1", message=True):
    query = {"id": callback_id, "from": {"id": user}, "data": data}
    if message:  # Telegram says which message carried the button
        query["message"] = {"message_id": 77, "chat": {"id": ME}}
    return {"update_id": 1, "callback_query": query}


@pytest.fixture
def telegram():
    with fake_telegram() as (fake, url):
        yield Telegram(TOKEN, base_url=url), fake


def tap_with(db, telegram, update, published=None):
    bot, _ = telegram
    handle_update(
        update, engine=db, bot=bot, allowed={ME}, publish_job=(published.append if published is not None else None)
    )


def test_a_tap_from_you_approves_publishes_and_tells_you_in_telegram(db, telegram):
    job_id = waiting_job(db)
    published: list[uuid.UUID] = []
    tap_with(db, telegram, tap(f"a:{job_id}"), published)
    fake = telegram[1]
    assert row(db, job_id)["stage"] == "publish" and published == [job_id]
    assert fake.calls_to("answerCallbackQuery")[0].fields["callback_query_id"] == "cb1"
    edit = fake.calls_to("editMessageCaption")[0].fields
    assert (
        (edit["chat_id"], edit["message_id"]) == (ME, 55)
        and "Approved" in edit["caption"]
        and "Judul klip" in edit["caption"]
    )


def test_a_tap_on_reject_finishes_the_job_and_publishes_nothing(db, telegram):
    job_id = waiting_job(db)
    published: list[uuid.UUID] = []
    tap_with(db, telegram, tap(f"r:{job_id}"), published)
    assert row(db, job_id)["data"]["review"]["state"] == "rejected" and published == []
    assert "Rejected" in telegram[1].calls_to("editMessageCaption")[0].fields["caption"]


def test_a_tap_from_anyone_else_changes_nothing_and_gets_no_answer(db, telegram):
    job_id = waiting_job(db)
    published: list[uuid.UUID] = []
    tap_with(db, telegram, tap(f"a:{job_id}", user=STRANGER), published)
    assert row(db, job_id)["status"] == "waiting" and published == [] and telegram[1].calls == []


@pytest.mark.parametrize(
    "data", ["", "a:", "a:not-a-uuid", f"x:{uuid.uuid4()}", f"a:{uuid.uuid4()}:more", "a" * 200, None]
)
def test_a_tap_with_nonsense_data_is_ignored(db, telegram, data):
    waiting_job(db)
    tap_with(db, telegram, tap(data))
    assert telegram[1].calls_to("editMessageCaption") == []


def test_a_tap_for_a_clip_that_is_already_decided_says_so(db, telegram):
    job_id = waiting_job(db)
    published: list[uuid.UUID] = []
    tap_with(db, telegram, tap(f"a:{job_id}"), published)
    tap_with(db, telegram, tap(f"r:{job_id}", callback_id="cb2"), published)
    assert row(db, job_id)["stage"] == "publish" and published == [job_id]
    assert "Already" in telegram[1].calls_to("answerCallbackQuery")[1].fields["text"]


def test_a_tap_for_a_clip_that_does_not_exist_says_so_in_a_popup_and_takes_the_buttons_away(db, telegram):
    tap_with(db, telegram, tap(f"a:{uuid.uuid4()}"))
    answer = telegram[1].calls_to("answerCallbackQuery")[0].fields
    assert "no longer" in answer["text"] and answer["show_alert"] is True
    edit = telegram[1].calls_to("editMessageCaption")[0].fields  # the message that was tapped, not a stored one
    assert (edit["chat_id"], edit["message_id"]) == (ME, 77) and "no longer" in edit["caption"]
    assert edit["reply_markup"] == {"inline_keyboard": []}


def test_a_tap_on_a_clip_that_is_no_longer_waiting_shows_how_it_ended_and_clears_the_buttons(db, telegram):
    job_id = waiting_job(db)
    tap_with(db, telegram, tap(f"a:{job_id}"))
    tap_with(db, telegram, tap(f"r:{job_id}", callback_id="cb2"))
    second = telegram[1].calls_to("editMessageCaption")[-1].fields
    assert (second["chat_id"], second["message_id"]) == (ME, 77) and "Approved" in second["caption"]


def test_a_tap_for_a_missing_clip_without_a_message_still_answers(db, telegram):
    tap_with(db, telegram, tap(f"a:{uuid.uuid4()}", message=False))
    assert "no longer" in telegram[1].calls_to("answerCallbackQuery")[0].fields["text"]
    assert telegram[1].calls_to("editMessageCaption") == []


def test_text_messages_to_the_bot_are_never_acted_on(db, telegram):
    job_id = waiting_job(db)
    tap_with(db, telegram, {"update_id": 2, "message": {"from": {"id": ME}, "text": f"approve {job_id}"}})
    assert row(db, job_id)["status"] == "waiting" and telegram[1].calls == []


def test_a_queue_that_is_down_does_not_undo_the_approval(db, telegram):
    job_id = waiting_job(db)

    def down(_):
        raise ConnectionError("redis is down")

    tap_with(db, telegram, tap(f"a:{job_id}"))  # no queue given: the job is simply left queued
    job_id = waiting_job(db)
    handle_update(tap(f"a:{job_id}"), engine=db, bot=telegram[0], allowed={ME}, publish_job=down)
    assert (row(db, job_id)["stage"], row(db, job_id)["status"]) == ("publish", "queued")  # the reconciler publishes it
    assert "Approved" in telegram[1].calls_to("editMessageCaption")[-1].fields["caption"]


# ---------- the Delete file button of an R2 link ------------------------------------------------------
def hosted_file(db):
    from services.publisher import hosted, posts

    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        job = jobs.create_job(conn, profiles[0], stage="publish")
        post, _ = posts.claim(conn, job, "tt_test", "tiktok")
        return hosted.add(conn, post["id"], "tiktok/a.mp4", 1000, [{"chat_id": ME, "message_id": 5}])


def test_pressing_delete_asks_for_the_file_to_be_deleted_without_this_service_touching_the_store(db, telegram):
    from services.publisher import hosted

    file_id = hosted_file(db)
    tap_with(db, telegram, tap(f"x:{file_id}"))
    with db.connect() as conn:
        due = hosted.due(conn, max_age_h=10**6)
    assert [(f.id, why) for f, why in due] == [(file_id, "asked")]
    assert "Deleting" in telegram[1].calls_to("answerCallbackQuery")[0].fields["text"]


def test_after_pressing_delete_the_message_loses_its_button(db, telegram):
    tap_with(db, telegram, tap(f"x:{hosted_file(db)}"))
    (edit,) = telegram[1].calls_to("editMessageText")
    assert "Deleting" in edit.fields["text"] and edit.fields["reply_markup"] == {"inline_keyboard": []}
    assert (edit.fields["chat_id"], edit.fields["message_id"]) == (ME, 77)


def test_pressing_delete_again_replaces_the_message_with_a_note_and_no_button(db, telegram):
    file_id = hosted_file(db)
    tap_with(db, telegram, tap(f"x:{file_id}"))
    tap_with(db, telegram, tap(f"x:{file_id}", callback_id="cb2"))
    last = telegram[1].calls_to("editMessageText")[-1]
    assert "already deleted" in last.fields["text"] and last.fields["reply_markup"] == {"inline_keyboard": []}


def test_pressing_delete_twice_or_for_a_file_that_is_gone_says_so(db, telegram):
    file_id = hosted_file(db)
    tap_with(db, telegram, tap(f"x:{file_id}"))
    tap_with(db, telegram, tap(f"x:{file_id}", callback_id="cb2"))
    tap_with(db, telegram, tap(f"x:{uuid.uuid4()}", callback_id="cb3"))
    answers = [c.fields["text"] for c in telegram[1].calls_to("answerCallbackQuery")]
    assert "Deleting" in answers[0] and "already" in answers[1].lower() and "already" in answers[2].lower()


def test_only_you_can_press_delete(db, telegram):
    from services.publisher import hosted

    file_id = hosted_file(db)
    tap_with(db, telegram, tap(f"x:{file_id}", user=STRANGER))
    with db.connect() as conn:
        assert hosted.due(conn, max_age_h=10**6) == [] and telegram[1].calls == []


# ---------- why a clip was rejected ----------------------------------------------------------------------------
def buttons_of(call):
    return [b["callback_data"] for row in call.fields["reply_markup"]["inline_keyboard"] for b in row]


def test_a_reject_tap_rejects_at_once_and_then_offers_the_reasons_on_the_message(db, telegram):
    job_id = waiting_job(db)
    tap_with(db, telegram, tap(f"r:{job_id}"))
    assert row(db, job_id)["data"]["review"]["state"] == "rejected"  # decided before any reason is given
    edit = telegram[1].calls_to("editMessageCaption")[0]
    codes = [c.split(":")[1] for c in buttons_of(edit)]
    assert {"boring", "cut", "layout", "audio", "topic", "dupe", "other", "skip"} == set(codes)
    assert all(c.endswith(str(job_id)) for c in buttons_of(edit)) and "why" in edit.fields["caption"].lower()


def test_tapping_a_reason_keeps_it_and_shows_it_on_the_message_without_buttons(db, telegram):
    job_id = waiting_job(db)
    tap_with(db, telegram, tap(f"r:{job_id}"))
    tap_with(db, telegram, tap(f"w:layout:{job_id}"))
    assert row(db, job_id)["data"]["review"]["reason"] == "layout"
    last = telegram[1].calls_to("editMessageCaption")[-1]
    assert "Rejected" in last.fields["caption"] and "Judul klip" in last.fields["caption"]
    assert "crop" in last.fields["caption"].lower() and last.fields["reply_markup"]["inline_keyboard"] == []


def test_the_first_reason_stays_and_a_second_tap_changes_nothing(db, telegram):
    job_id = waiting_job(db)
    tap_with(db, telegram, tap(f"r:{job_id}"))
    tap_with(db, telegram, tap(f"w:boring:{job_id}"))
    tap_with(db, telegram, tap(f"w:cut:{job_id}"))
    assert row(db, job_id)["data"]["review"]["reason"] == "boring"
    assert "already" in telegram[1].calls_to("answerCallbackQuery")[-1].fields["text"].lower()


def test_skip_gives_no_reason_and_takes_the_buttons_away(db, telegram):
    job_id = waiting_job(db)
    tap_with(db, telegram, tap(f"r:{job_id}"))
    tap_with(db, telegram, tap(f"w:skip:{job_id}"))
    assert "reason" not in row(db, job_id)["data"]["review"]
    assert telegram[1].calls_to("editMessageCaption")[-1].fields["reply_markup"]["inline_keyboard"] == []


def test_a_reason_cannot_be_given_for_a_clip_that_was_not_rejected(db, telegram):
    job_id = waiting_job(db)
    tap_with(db, telegram, tap(f"a:{job_id}"))
    tap_with(db, telegram, tap(f"w:boring:{job_id}"))
    assert "reason" not in row(db, job_id)["data"]["review"]


def test_an_unknown_reason_or_a_stranger_changes_nothing(db, telegram):
    job_id = waiting_job(db)
    tap_with(db, telegram, tap(f"r:{job_id}"))
    tap_with(db, telegram, tap(f"w:whatever:{job_id}"))
    bot, _ = telegram
    handle_update(tap(f"w:boring:{job_id}", user=STRANGER), engine=db, bot=bot, allowed={ME}, publish_job=None)
    assert "reason" not in row(db, job_id)["data"]["review"]


# ---------- what happens to the stored copy when you decide ----------
from fake_s3_server import fake_s3  # noqa: E402

from common.r2 import R2  # noqa: E402
from services.publisher import accounts as acc  # noqa: E402
from services.publisher.clipstore import ClipStore  # noqa: E402

HOST = acc.MediaHost(
    bucket_env="B", account_id_env="A", access_key_env="K", secret_key_env="S",  # noqa: S106
    link_ttl_min=2880, keep_after_approval_h=24, max_age_h=48, max_total_gb=8,
)  # fmt: skip


@pytest.fixture
def stored_clip(db, tmp_path):
    """A job waiting for review whose full quality copy is in R2, as the review service leaves it."""
    job_id = waiting_job(db)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"v" * 3000)
    with fake_s3() as (s3, url):
        secret = "secret-for-tests"  # noqa: S105
        clips = ClipStore(
            db, R2(account_id="a", bucket="clips", access_key="ak", secret_key=secret, endpoint=url), HOST
        )
        clips.put(job_id, clip, pending_h=24)
        yield job_id, s3, clips


def tap_clips(db, telegram, update, clips):
    bot, _ = telegram
    handle_update(update, engine=db, bot=bot, allowed={ME}, publish_job=lambda j: None, clips=clips)


def hosted_row(db):
    with db.connect() as conn:
        return (
            conn.execute(
                sa.text(
                    "SELECT deleted_at, EXTRACT(EPOCH FROM keep_until - clock_timestamp()) / 3600 AS hours "
                    "FROM hosted_files"
                )
            )
            .mappings()
            .one()
        )


def test_rejecting_removes_the_stored_copy_at_once(db, telegram, stored_clip):
    job_id, s3, clips = stored_clip
    tap_clips(db, telegram, tap(f"r:{job_id}"), clips)
    assert s3.objects == {} and hosted_row(db)["deleted_at"] is not None


def test_approving_keeps_the_stored_copy_for_the_set_time_and_leaves_the_link_on_the_message(db, telegram, stored_clip):
    job_id, s3, clips = stored_clip
    tap_clips(db, telegram, tap(f"a:{job_id}"), clips)
    assert len(s3.objects) == 1 and hosted_row(db)["hours"] == pytest.approx(24, abs=0.1)
    edit = telegram[1].calls_to("editMessageCaption")[-1].fields
    assert "Approved" in edit["caption"]
    (button,) = edit["reply_markup"]["inline_keyboard"][0]
    assert button["text"].startswith("⬇") and "X-Amz-Signature" in button["url"]  # still there to download from


def test_a_review_that_expires_removes_the_stored_copy_too(db, telegram, stored_clip):
    from services.review.listener import finish_expired

    job_id, s3, clips = stored_clip
    with db.begin() as conn:
        conn.execute(
            sa.text("UPDATE jobs SET updated_at = clock_timestamp() - interval '30 hours' WHERE id = :i"), {"i": job_id}
        )
    finish_expired(db, telegram[0], clips)
    assert row(db, job_id)["data"]["review"]["state"] == "expired" and s3.objects == {}


def test_a_stored_copy_past_its_time_is_swept_while_the_service_runs(db, telegram, stored_clip):
    from services.publisher import hosted
    from services.review.listener import finish_expired

    job_id, s3, clips = stored_clip
    with db.begin() as conn:
        hosted.set_keep(conn, f"review/{job_id}.mp4", -1)
    finish_expired(db, telegram[0], clips)
    assert s3.objects == {}


def test_decisions_work_as_before_without_a_store(db, telegram):
    job_id = waiting_job(db)
    tap_with(db, telegram, tap(f"r:{job_id}"))
    assert row(db, job_id)["data"]["review"]["state"] == "rejected"
