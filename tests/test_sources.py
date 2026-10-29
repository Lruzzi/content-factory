"""Sources live in the database: seeded from YAML, validated on write, guarded for Hermes (FR-1, FR-10, FR-19)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import sources

pytestmark = pytest.mark.integration

CONFIG = cs.ROOT
CAM = {"layout": {"cam": {"x": 1400, "y": 700, "w": 500, "h": 340}}}


def add(db, url="https://www.youtube.com/@newcreator", **kw):
    options = {"profile": "mlbb", "overrides": CAM, "updated_by": "tester"} | kw
    with db.begin() as conn:
        return sources.add_source(conn, CONFIG, url=url, **options)


def count(db):
    with db.connect() as conn:
        return conn.execute(sa.text("SELECT count(*) FROM sources")).scalar_one()


def row(db, source_id):
    with db.connect() as conn:
        return conn.execute(sa.text("SELECT * FROM sources WHERE id = :i"), {"i": source_id}).mappings().one()


# ---------- seeding ----------------------------------------------------------------------------
def test_an_empty_database_is_seeded_from_the_yaml_sources(db):
    with db.begin() as conn:
        seeded = sources.seed_from_yaml(conn, CONFIG)
    assert seeded == count(db) > 0
    with db.connect() as conn:
        keys = {r["source_key"] for r in sources.list_sources(conn, "mlbb")}
    assert "leokabuki" in keys


def test_a_database_that_already_has_sources_is_never_seeded_again(db):
    add(db)
    with db.begin() as conn:
        assert sources.seed_from_yaml(conn, CONFIG) == 0
    assert count(db) == 1


def test_seeding_twice_adds_nothing_the_second_time(db):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, CONFIG)
    first = count(db)
    with db.begin() as conn:
        assert sources.seed_from_yaml(conn, CONFIG) == 0
    assert count(db) == first


# ---------- adding and validating ---------------------------------------------------------------
def test_a_valid_source_is_stored_with_who_added_it(db):
    created = add(db)
    assert created["source_key"] == "newcreator"
    assert created["updated_by"] == "tester"
    assert created["enabled"] is True
    assert created["clip_policy"] == "open"


def test_a_bad_link_is_rejected_and_nothing_is_stored(db):
    with pytest.raises(ValueError, match="unsupported source url"):
        add(db, url="https://example.com/watch")
    assert count(db) == 0


def test_a_split_layout_source_needs_its_cam_box(db):
    with pytest.raises(ValueError, match=r"layout\.cam"):
        add(db, overrides={})
    assert count(db) == 0


def test_an_override_that_breaks_the_profile_is_rejected(db):
    with pytest.raises(ValueError, match="clip_length_s"):
        add(db, overrides=CAM | {"selection": {"clip_length_s": [90, 20]}})
    assert count(db) == 0


def test_an_unknown_profile_is_rejected(db):
    with pytest.raises(ValueError, match="unknown profile"):
        add(db, profile="nope")


def test_the_same_creator_cannot_be_added_twice_to_a_profile(db):
    add(db)
    with pytest.raises(ValueError, match="already exists"):
        add(db, url="https://www.youtube.com/@newcreator/streams")
    assert count(db) == 1


def test_the_operator_may_add_a_blocked_source(db):
    assert add(db, clip_policy="blocked")["clip_policy"] == "blocked"


# ---------- loading profiles from the database -------------------------------------------------------
def test_profiles_are_built_from_database_rows(db):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, CONFIG)
    with db.connect() as conn:
        profiles, problems = sources.load_profiles(conn, CONFIG)
    assert problems == []
    ids = {(p.name, p.source.id) for p in profiles}
    assert ("mlbb", "leokabuki") in ids


def test_a_bad_row_is_skipped_with_a_problem_and_the_rest_still_load(db):
    add(db)
    with db.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO sources (profile, source_key, url) VALUES ('mlbb', 'broken', 'https://example.com/x')")
        )
    with db.connect() as conn:
        profiles, problems = sources.load_profiles(conn, CONFIG)
    assert [p.source.id for p in profiles] == ["newcreator"]
    assert len(problems) == 1
    assert "broken" in problems[0]


def test_a_row_for_a_profile_with_no_file_is_a_problem_not_a_crash(db):
    with db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO sources (profile, source_key, url) VALUES ('ghost', 'x', 'https://www.youtube.com/@x')"
            )
        )
    with db.connect() as conn:
        profiles, problems = sources.load_profiles(conn, CONFIG)
    assert profiles == []
    assert "ghost" in problems[0]


# ---------- pausing, claims and what Hermes may not undo -------------------------------------------------
def test_a_source_can_be_paused_and_resumed(db):
    created = add(db)
    with db.begin() as conn:
        sources.pause(conn, created["id"], updated_by="hermes")
    paused = row(db, created["id"])
    assert paused["paused_at"] is not None
    assert paused["pause_reason"] == "manual"
    with db.begin() as conn:
        sources.resume(conn, created["id"], updated_by="hermes")
    assert row(db, created["id"])["paused_at"] is None


def test_a_claim_pauses_the_source_and_counts_it(db):
    created = add(db)
    with db.begin() as conn:
        sources.record_claim(conn, created["id"])
        sources.record_claim(conn, created["id"])
    r = row(db, created["id"])
    assert (r["claim_count"], r["pause_reason"]) == (2, "claim")
    assert r["paused_at"] is not None


def test_resuming_a_claim_pause_needs_the_operator_not_hermes(db):
    created = add(db)
    with db.begin() as conn:
        sources.record_claim(conn, created["id"])
    with pytest.raises(PermissionError), db.begin() as conn:
        sources.resume(conn, created["id"], updated_by="hermes")
    assert row(db, created["id"])["paused_at"] is not None
    with db.begin() as conn:
        sources.resume(conn, created["id"], updated_by="operator", allow_claim=True)
    assert row(db, created["id"])["paused_at"] is None


def test_a_manual_pause_does_not_hide_a_claim(db):
    created = add(db)
    with db.begin() as conn:
        sources.record_claim(conn, created["id"])
        sources.pause(conn, created["id"], updated_by="hermes")
    assert row(db, created["id"])["pause_reason"] == "claim"


def test_a_blocked_source_is_not_reopened_without_the_operator(db):
    created = add(db, clip_policy="blocked")
    with pytest.raises(PermissionError), db.begin() as conn:
        sources.update_source(conn, CONFIG, created["id"], changes={"clip_policy": "open"}, updated_by="hermes")
    assert row(db, created["id"])["clip_policy"] == "blocked"
    with db.begin() as conn:
        sources.update_source(
            conn, CONFIG, created["id"], changes={"clip_policy": "open"}, updated_by="operator", allow_reopen=True
        )
    assert row(db, created["id"])["clip_policy"] == "open"


def test_blocking_a_source_is_always_allowed(db):
    created = add(db)
    with db.begin() as conn:
        sources.update_source(conn, CONFIG, created["id"], changes={"clip_policy": "blocked"}, updated_by="hermes")
    assert row(db, created["id"])["clip_policy"] == "blocked"


def test_an_update_is_validated_and_a_bad_one_changes_nothing(db):
    created = add(db)
    with pytest.raises(ValueError, match="layout"), db.begin() as conn:
        sources.update_source(
            conn, CONFIG, created["id"], changes={"overrides": {"layout": {"cam": {"x": 1}}}}, updated_by="hermes"
        )
    assert row(db, created["id"])["overrides"] == CAM


def test_an_update_records_who_changed_it_and_when(db):
    created = add(db)
    with db.begin() as conn:
        sources.update_source(conn, CONFIG, created["id"], changes={"name": "Renamed"}, updated_by="hermes")
    r = row(db, created["id"])
    assert (r["name"], r["updated_by"]) == ("Renamed", "hermes")
    assert r["updated_at"] > created["updated_at"]


def test_only_known_fields_can_be_updated(db):
    created = add(db)
    with pytest.raises(ValueError, match="cannot be changed"), db.begin() as conn:
        sources.update_source(conn, CONFIG, created["id"], changes={"claim_count": 0}, updated_by="hermes")


def test_there_is_no_way_to_delete_a_source():
    assert not hasattr(sources, "delete_source")
