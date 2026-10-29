"""The matches the LLM found in a stream, remembered so a match is never turned into a recap twice."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from db_helpers import CHECK, UNIQUE, violation

import config_schema as cs
from common import jobs, sources

pytestmark = pytest.mark.integration


@pytest.fixture
def stream(db):
    with db.begin() as conn:
        return conn.execute(
            sa.text("INSERT INTO live_streams (video_id) VALUES ('vid00001') RETURNING id")
        ).scalar_one()


def add(db, stream, start=100, end=1500, status="detected"):
    with db.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO matches (stream_id, start_s, end_s, status) VALUES (:s, :a, :b, :st)"),
            {"s": stream, "a": start, "b": end, "st": status},
        )


def test_a_recap_is_an_allowed_kind_of_job_and_an_invented_kind_is_not(db):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        profile = next(p for p in profiles if p.source.id == "leokabuki")
        assert jobs.create_job(conn, profile, stage="render", kind="recap")
    with violation(CHECK), db.begin() as conn:
        jobs.create_job(conn, profile, stage="render", kind="montage")


def test_a_match_is_stored_with_its_stream_and_times(db, stream):
    add(db, stream)
    with db.connect() as conn:
        row = conn.execute(sa.text("SELECT status, start_s, end_s FROM matches")).one()
    assert row.status == "detected" and float(row.start_s) == 100 and float(row.end_s) == 1500


def test_the_same_match_cannot_be_stored_twice(db, stream):
    add(db, stream)
    with violation(UNIQUE):
        add(db, stream)


def test_a_match_must_end_after_it_starts_and_have_a_known_status(db, stream):
    with violation(CHECK):
        add(db, stream, start=500, end=500)
    with violation(CHECK):
        add(db, stream, start=0, end=10, status="maybe")


def test_deleting_a_stream_removes_its_matches(db, stream):
    add(db, stream)
    with db.begin() as conn:
        conn.execute(sa.text("DELETE FROM live_streams WHERE id = :s"), {"s": stream})
        assert conn.execute(sa.text("SELECT count(*) FROM matches")).scalar_one() == 0
