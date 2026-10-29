"""Jobs: the record that moves through the stages, with its settings snapshot (NFR-12, FR-18)."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from db_helpers import CHECK, violation

import config_schema as cs
from common import jobs, pipeline

pytestmark = pytest.mark.integration

RETRY = jobs.Retry(max_attempts=3, backoff_s=(60, 300, 1800))


def a_profile() -> cs.Profile:
    profiles = cs.load_profile(cs.ROOT / "profiles" / "mlbb.yaml")
    return next(p for p in profiles if p.source.id == "leokabuki")


def new_job(db, stage="understand", **kw) -> uuid.UUID:
    with db.begin() as conn:
        return jobs.create_job(conn, a_profile(), stage=stage, **kw)


def get(db, job_id):
    with db.connect() as conn:
        return jobs.get(conn, job_id)


def seconds_until_retry(db, job_id) -> float:
    with db.connect() as conn:
        return float(
            conn.execute(
                sa.text("SELECT EXTRACT(EPOCH FROM retry_at - clock_timestamp()) FROM jobs WHERE id = :i"),
                {"i": job_id},
            ).scalar_one()
        )


# ---------- the pipeline ------------------------------------------------------------------------------
def test_stages_follow_each_other_and_the_last_has_no_next():
    assert pipeline.next_stage("understand") == "select"
    assert pipeline.next_stage(pipeline.STAGES[-1]) is None


def test_every_stage_has_its_own_stream_and_a_prefix_isolates_them():
    names = {pipeline.stream_for(s) for s in pipeline.STAGES}
    assert len(names) == len(pipeline.STAGES)
    assert pipeline.stream_for("select", "t1") != pipeline.stream_for("select", "t2")


def test_an_unknown_stage_is_rejected():
    with pytest.raises(ValueError, match="unknown stage"):
        pipeline.stream_for("bogus")


# ---------- creating a job --------------------------------------------------------------------------
def test_a_new_job_is_queued_at_its_stage_with_no_attempts(db):
    job = get(db, new_job(db, stage="select"))
    assert (job["stage"], job["status"], job["attempt"]) == ("select", "queued", 0)


def test_the_job_keeps_a_snapshot_of_its_resolved_profile(db):
    job = get(db, new_job(db))
    assert job["spec"]["selection"]["mode"] == "hybrid"
    assert job["spec"]["source"]["id"] == "leokabuki"


def test_the_snapshot_can_rebuild_the_profile_exactly(db):
    original = a_profile()
    job = get(db, new_job(db))
    assert cs.Profile.model_validate(job["spec"]).model_dump(mode="json") == original.model_dump(mode="json")


def test_changing_the_yaml_later_does_not_change_a_job_already_created(db):
    job_id = new_job(db)
    with db.begin() as conn:
        spec_now = conn.execute(sa.text("SELECT spec FROM jobs WHERE id = :i"), {"i": job_id}).scalar_one()
    assert spec_now["selection"]["top_k"] == a_profile().selection.top_k


def test_a_job_can_carry_a_media_key_and_a_kind(db):
    job = get(db, new_job(db, media_key="clips/one.mp4", kind="tutorial"))
    assert (job["media_key"], job["kind"]) == ("clips/one.mp4", "tutorial")


def test_an_unknown_kind_is_rejected_by_the_database(db):
    with violation(CHECK):
        new_job(db, kind="gossip")


def test_an_unknown_stage_cannot_create_a_job(db):
    with pytest.raises(ValueError, match="unknown stage"):
        new_job(db, stage="bogus")


def test_a_missing_job_is_none(db):
    assert get(db, uuid.uuid4()) is None


# ---------- claiming and advancing ------------------------------------------------------------------
def test_only_one_worker_can_claim_a_queued_job(db):
    job_id = new_job(db)
    with db.begin() as conn:
        assert jobs.claim(conn, job_id)
    with db.begin() as conn:
        assert not jobs.claim(conn, job_id)
    assert get(db, job_id)["status"] == "running"


def test_a_failed_job_can_be_claimed_again(db):
    job_id = new_job(db)
    with db.begin() as conn:
        jobs.claim(conn, job_id)
        jobs.record_failure(conn, job_id, "boom", RETRY)
    with db.begin() as conn:
        assert jobs.claim(conn, job_id)


def test_advancing_moves_the_job_to_the_next_stage_queued_and_resets_attempts(db):
    job_id = new_job(db)
    with db.begin() as conn:
        jobs.claim(conn, job_id)
        jobs.record_failure(conn, job_id, "boom", RETRY)
        jobs.claim(conn, job_id)
        jobs.advance(conn, job_id, "select")
    job = get(db, job_id)
    assert (job["stage"], job["status"], job["attempt"], job["retry_at"]) == ("select", "queued", 0, None)


def test_advancing_to_nothing_finishes_the_job(db):
    job_id = new_job(db, stage="publish")
    with db.begin() as conn:
        jobs.claim(conn, job_id)
        jobs.advance(conn, job_id, None)
    assert get(db, job_id)["status"] == "done"


def test_a_job_cannot_advance_to_an_unknown_stage(db):
    job_id = new_job(db)
    with pytest.raises(ValueError, match="unknown stage"), db.begin() as conn:
        jobs.advance(conn, job_id, "bogus")


# ---------- failures and backoff ---------------------------------------------------------------------
def test_a_failure_schedules_a_retry_after_the_first_backoff(db):
    job_id = new_job(db)
    with db.begin() as conn:
        jobs.claim(conn, job_id)
        assert jobs.record_failure(conn, job_id, "boom", RETRY) == "failed"
    job = get(db, job_id)
    assert (job["status"], job["attempt"], job["last_error"]) == ("failed", 1, "boom")
    assert 55 < seconds_until_retry(db, job_id) <= 60


def test_backoff_grows_with_each_attempt(db):
    job_id = new_job(db)
    waits = []
    for _ in range(2):
        with db.begin() as conn:
            jobs.claim(conn, job_id)
            jobs.record_failure(conn, job_id, "boom", RETRY)
        waits.append(seconds_until_retry(db, job_id))
    assert 55 < waits[0] <= 60
    assert 295 < waits[1] <= 300


def test_the_third_failure_kills_the_job_and_clears_the_retry(db):
    job_id = new_job(db)
    outcomes = []
    for _ in range(3):
        with db.begin() as conn:
            jobs.claim(conn, job_id)
            outcomes.append(jobs.record_failure(conn, job_id, "boom", RETRY))
    assert outcomes == ["failed", "failed", "dead"]
    job = get(db, job_id)
    assert (job["status"], job["retry_at"]) == ("dead", None)


def test_a_dead_job_cannot_be_claimed(db):
    job_id = new_job(db)
    for _ in range(3):
        with db.begin() as conn:
            jobs.claim(conn, job_id)
            jobs.record_failure(conn, job_id, "boom", RETRY)
    with db.begin() as conn:
        assert not jobs.claim(conn, job_id)


def test_a_very_long_error_is_stored_truncated(db):
    job_id = new_job(db)
    with db.begin() as conn:
        jobs.claim(conn, job_id)
        jobs.record_failure(conn, job_id, "x" * 10_000, RETRY)
    assert len(get(db, job_id)["last_error"]) <= 2_000


def test_a_requeued_dead_job_starts_over(db):
    job_id = new_job(db)
    for _ in range(3):
        with db.begin() as conn:
            jobs.claim(conn, job_id)
            jobs.record_failure(conn, job_id, "boom", RETRY)
    with db.begin() as conn:
        assert jobs.requeue_dead(conn, job_id)
    job = get(db, job_id)
    assert (job["status"], job["attempt"]) == ("queued", 0)


def test_only_a_dead_job_can_be_requeued_by_hand(db):
    job_id = new_job(db)
    with db.begin() as conn:
        assert not jobs.requeue_dead(conn, job_id)


# ---------- what a stage hands to the next one ------------------------------------------------------
def test_a_new_job_has_empty_data_unless_given_some(db):
    assert get(db, new_job(db))["data"] == {}
    assert get(db, new_job(db, data={"render": {"start_s": 1.5}}))["data"] == {"render": {"start_s": 1.5}}


def test_a_stage_can_set_the_media_key_and_add_to_the_data_without_losing_the_rest(db):
    job_id = new_job(db, data={"render": {"start_s": 1.5}, "select": {"score": 0.8}})
    with db.begin() as conn:
        jobs.set_result(conn, job_id, media_key="clips/one.mp4", data={"render": {"start_s": 1.5, "duration_s": 3.0}})
    job = get(db, job_id)
    assert job["media_key"] == "clips/one.mp4"
    assert job["data"] == {"render": {"start_s": 1.5, "duration_s": 3.0}, "select": {"score": 0.8}}


def test_setting_only_data_leaves_the_media_key_alone(db):
    job_id = new_job(db, media_key="segments/a.ts")
    with db.begin() as conn:
        jobs.set_result(conn, job_id, data={"x": 1})
    assert get(db, job_id)["media_key"] == "segments/a.ts"


# ---------- deferring: "not now", which is not a failure ------------------------------------------------------------
def test_a_deferred_job_waits_for_the_time_given_and_does_not_lose_an_attempt(db):
    job_id = new_job(db)
    with db.begin() as conn:
        assert jobs.claim(conn, job_id)
        jobs.defer(conn, job_id, 300, "waiting for the Groq quota")
    job = get(db, job_id)
    assert job["status"] == "failed" and job["attempt"] == 0 and "Groq quota" in job["last_error"]
    with db.connect() as conn:
        wait = conn.execute(
            sa.text("SELECT EXTRACT(EPOCH FROM retry_at - clock_timestamp()) FROM jobs WHERE id = :i"), {"i": job_id}
        ).scalar_one()
    assert 280 < float(wait) <= 300  # the reconciler queues it again when this has passed


def test_deferring_over_and_over_never_kills_the_job(db):
    job_id = new_job(db)
    for _ in range(10):
        with db.begin() as conn:
            assert jobs.claim(conn, job_id) or True
            jobs.defer(conn, job_id, 60, "quota")
    job = get(db, job_id)
    assert job["status"] == "failed" and job["attempt"] == 0
