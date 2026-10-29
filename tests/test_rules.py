"""Posting and production rules changed from Telegram: parsed strictly, kept in `settings`, never above the ceilings."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import pytest
import sqlalchemy as sa

from common import rules

CEIL = rules.Ceilings(daily_cap=20, min_spacing_min=720, clips_per_day=40)


@dataclass(frozen=True)
class Acc:
    id: str = "yt_test"
    daily_cap: int = 6
    min_spacing_min: int = 120
    post_windows: tuple = ("10:00-23:00",)


# ---------- typed values ----------
@pytest.mark.parametrize(
    ("name", "text", "value"),
    [
        ("cap", "6", 6), ("cap", " 20 ", 20), ("spacing", "0", 0), ("spacing", "720", 720), ("clips", "0", 0),
        ("clips", "12", 12), ("window", "10:00-23:00", ["10:00-23:00"]),
        ("window", "10:00-13:00, 18:00-23:00", ["10:00-13:00", "18:00-23:00"]), ("window", "ANY", []),
        ("window", "22:00-02:00", ["22:00-02:00"]),
    ],
)  # fmt: skip
def test_a_good_value_is_read(name, text, value):
    assert rules.parse(name, text, CEIL) == value


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("cap", "0"), ("cap", "21"), ("cap", "-1"), ("cap", "six"), ("cap", "6.5"), ("cap", ""), ("cap", "99999"),
        ("spacing", "721"), ("clips", "41"), ("window", "10-23"), ("window", "25:00-26:00"), ("window", "10:00-10:00"),
        ("window", "10:00-11:00,11:00-12:00,12:00-13:00,13:00-14:00,14:00-15:00"), ("window", "now"),
    ],
)  # fmt: skip
def test_a_value_outside_what_the_ceilings_allow_or_not_a_number_is_refused(name, text):
    with pytest.raises(ValueError):
        rules.parse(name, text, CEIL)


def test_a_value_survives_the_trip_through_a_button():
    for name, value in (("cap", 6), ("window", ["10:00-13:00", "18:00-23:00"]), ("window", [])):
        text = rules.encode(name, value) or "any"
        assert len(text.encode()) < 40
        assert rules.decode(name, text, CEIL) == value


def test_the_ceilings_come_from_workers_yaml():
    assert rules.ceilings_from(
        {"rules": {"ceilings": {"daily_cap": 9, "min_spacing_min": 100, "clips_per_day": 8}}}
    ) == (rules.Ceilings(9, 100, 8))
    assert rules.ceilings_from({}) is None


# ---------- kept in the database ----------
pytestmark_db = pytest.mark.integration


@pytest.mark.integration
def test_without_a_setting_the_account_keeps_what_config_gave_it(db):
    with db.connect() as conn:
        assert rules.apply(conn, Acc()) == Acc()
        assert rules.clips_per_day(conn, 6) == 6


@pytest.mark.integration
def test_a_setting_wins_over_config_for_every_account_and_says_who_set_it(db):
    with db.begin() as conn:
        rules.set_value(conn, "cap", 4, CEIL, "telegram:1")
        rules.set_value(conn, "spacing", 90, CEIL, "telegram:1")
        rules.set_value(conn, "window", ["09:00-12:00"], CEIL, "telegram:1")
        rules.set_value(conn, "clips", 8, CEIL, "telegram:2")
    with db.connect() as conn:
        for acc in (Acc(), Acc(id="fb_test", daily_cap=3)):
            got = rules.apply(conn, acc)
            assert (got.daily_cap, got.min_spacing_min, got.post_windows) == (
                4,
                90,
                ["09:00-12:00"],
            ) and got.id == acc.id
        assert rules.clips_per_day(conn, 6) == 8
        assert rules.overrides(conn)["clips"] == (8, "telegram:2")


@pytest.mark.integration
def test_setting_it_again_replaces_it_and_a_refused_value_changes_nothing(db):
    with db.begin() as conn:
        rules.set_value(conn, "cap", 4, CEIL, "a")
        rules.set_value(conn, "cap", 5, CEIL, "b")
    with db.connect() as conn:
        assert rules.apply(conn, Acc()).daily_cap == 5
    with pytest.raises(ValueError), db.begin() as conn:
        rules.set_value(conn, "cap", 21, CEIL, "c")
    with db.connect() as conn:
        assert rules.apply(conn, Acc()).daily_cap == 5 and rules.overrides(conn)["cap"][1] == "b"


@pytest.mark.integration
def test_a_hand_edited_row_that_makes_no_sense_is_ignored(db):
    with db.begin() as conn:
        for key, value in (
            ("rules.daily_cap", '"lots"'),
            ("rules.post_windows", '["nonsense"]'),
            ("rules.clips_per_day", "-3"),
        ):
            conn.execute(
                sa.text("INSERT INTO settings (key, value) VALUES (:k, CAST(:v AS jsonb))"), {"k": key, "v": value}
            )
    with db.connect() as conn:
        assert rules.apply(conn, Acc()) == Acc() and rules.clips_per_day(conn, 6) == 6


@pytest.mark.integration
def test_zero_clips_a_day_means_no_limit(db):
    with db.begin() as conn:
        rules.set_value(conn, "clips", 0, CEIL, "t")
    with db.connect() as conn:
        assert rules.clips_per_day(conn, 6) == 0


def clip_job(conn, *, review=None, hours_ago=0.0, test=False, blocked=False):
    data = {"clip": {"start_s": 1, "end_s": 20} | ({"test": True} if test else {})} | (
        {"review": {"state": review}} if review else {}
    )
    conn.execute(
        sa.text(
            "INSERT INTO jobs (id, stage, spec, data, created_at) "
            "VALUES (:i, 'review', CAST(:sp AS jsonb), CAST(:d AS jsonb), "
            "clock_timestamp() - make_interval(secs => :s))"
        ),
        {
            "i": uuid.uuid4(),
            "d": json.dumps(data),
            "s": hours_ago * 3600,
            "sp": json.dumps({"source": {"clip_policy": "blocked" if blocked else "open"}}),
        },  # fmt: skip
    )


@pytest.mark.integration
def test_clips_made_counts_the_last_24_hours_and_not_the_rejected(db):
    with db.begin() as conn:
        clip_job(conn)
        clip_job(conn, review="approved", hours_ago=10)
        clip_job(conn, review="pending", hours_ago=23)
        clip_job(conn, review="rejected", hours_ago=1)  # not going to be posted: it frees its place
        clip_job(conn, hours_ago=30)  # a day and more ago
        clip_job(conn, test=True)  # made for a test: never posted, so it takes no place in the day
        clip_job(conn, blocked=True)  # of a blocked source (a replay): never posted either
        conn.execute(sa.text("INSERT INTO jobs (stage, spec) VALUES ('select', '{}'::jsonb)"))  # not a clip
    with db.connect() as conn:
        assert rules.clips_made(conn) == 3


# ---------- the config ----------
def test_the_repos_workers_yaml_has_ceilings_that_cover_the_starting_values():
    import yaml

    import config_schema as cs

    workers = yaml.safe_load((cs.ROOT / "workers.yaml").read_text())
    ceilings = rules.ceilings_from(workers)
    accounts = yaml.safe_load((cs.ROOT / "accounts.yaml").read_text())["defaults"]
    assert ceilings is not None
    assert accounts["daily_cap"] <= ceilings.daily_cap and accounts["min_spacing_min"] <= ceilings.min_spacing_min
    defaults = yaml.safe_load((cs.ROOT / "profiles" / "_defaults.yaml").read_text())
    assert defaults["selection"]["clips_per_day"] <= ceilings.clips_per_day


@pytest.mark.parametrize(
    "bad",
    [
        {"daily_cap": 0, "min_spacing_min": 1, "clips_per_day": 1},
        {"daily_cap": 1},
        {"daily_cap": "x", "min_spacing_min": 1, "clips_per_day": 1},
    ],
)
def test_wrong_ceilings_are_refused(tmp_path, bad):
    import config_schema as cs
    from profile_factory import workers_file

    path = workers_file(tmp_path, lambda w: w.__setitem__("rules", {"ceilings": bad}))
    with pytest.raises(ValueError, match="rules"):
        cs.validate_workers(path)
