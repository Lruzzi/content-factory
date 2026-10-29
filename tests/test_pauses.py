"""A pause is a note in Postgres that something is used up until a time: every service sees the same one."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from common import pauses

pytestmark = pytest.mark.integration


def shift(db, key, seconds):
    """Move a pause's end `seconds` into the past (negative: the future), as if that much time had gone by."""
    with db.begin() as conn:
        conn.execute(
            sa.text("UPDATE pauses SET until = until - make_interval(secs => :s) WHERE key = :k"),
            {"s": seconds, "k": key},
        )


def test_nothing_is_paused_until_something_is(db):
    with db.connect() as conn:
        assert pauses.active(conn, "llm:x") is None


def test_the_first_pause_is_new_and_is_active_for_the_time_asked(db):
    with db.begin() as conn:
        assert pauses.start(conn, "llm:x", 600, "quota used up") is True
    with db.connect() as conn:
        pause = pauses.active(conn, "llm:x")
    assert pause is not None and pause.reason == "quota used up" and 590 <= pause.remaining_s <= 600


def test_a_second_trip_while_it_is_paused_changes_nothing_and_is_not_new(db):
    with db.begin() as conn:
        pauses.start(conn, "llm:x", 600, "first")
    with db.begin() as conn:
        assert pauses.start(conn, "llm:x", 99999, "second") is False
    with db.connect() as conn:
        pause = pauses.active(conn, "llm:x")
    assert pause is not None and pause.reason == "first" and pause.remaining_s <= 600


def test_a_pause_that_ended_a_little_ago_is_the_same_trouble_so_it_is_not_new(db):
    with db.begin() as conn:
        pauses.start(conn, "llm:x", 600, "first")
    shift(db, "llm:x", 700)  # ended 100 s ago
    with db.begin() as conn:
        assert pauses.start(conn, "llm:x", 600, "again") is False
        assert pauses.active(conn, "llm:x") is not None  # but it is paused again


def test_a_pause_that_ended_long_ago_is_new_trouble(db):
    with db.begin() as conn:
        pauses.start(conn, "llm:x", 600, "first")
    shift(db, "llm:x", 600 + 7 * 3600)
    with db.begin() as conn:
        assert pauses.start(conn, "llm:x", 600, "later") is True


def test_pauses_are_per_key(db):
    with db.begin() as conn:
        pauses.start(conn, "llm:a", 600, "a")
        assert pauses.active(conn, "llm:b") is None


def test_a_pause_that_has_run_out_is_no_longer_active(db):
    with db.begin() as conn:
        pauses.start(conn, "llm:x", 600, "first")
    shift(db, "llm:x", 601)
    with db.connect() as conn:
        assert pauses.active(conn, "llm:x") is None


def test_the_table_has_a_tenant_like_every_other(db):
    with db.connect() as conn:
        columns = {
            r[0]
            for r in conn.execute(
                sa.text("SELECT column_name FROM information_schema.columns WHERE table_name = 'pauses'")
            )
        }
    assert {"tenant_id", "key", "until", "reason"} <= columns


# ---------- switches you flip by hand: held until you release them -----------------------------------------------
def test_a_held_pause_has_no_end_until_it_is_released(db):
    with db.begin() as conn:
        pauses.hold(conn, "switch:publish", "paused from Telegram")
    with db.connect() as conn:
        pause = pauses.active(conn, "switch:publish")
    assert pause is not None and pause.reason == "paused from Telegram" and pause.remaining_s == float("inf")
    with db.begin() as conn:
        assert pauses.release(conn, "switch:publish") is True
        assert pauses.release(conn, "switch:publish") is False  # nothing left to release
    with db.connect() as conn:
        assert pauses.active(conn, "switch:publish") is None


def test_holding_twice_keeps_one_pause_and_releasing_one_leaves_the_other(db):
    with db.begin() as conn:
        pauses.hold(conn, "switch:publish", "a")
        pauses.hold(conn, "switch:publish", "b")
        pauses.hold(conn, "switch:intake", "c")
        assert pauses.release(conn, "switch:publish") is True
        assert pauses.active(conn, "switch:intake") is not None


def test_which_switches_are_on_comes_back_as_names(db):
    with db.begin() as conn:
        assert pauses.switches_on(conn) == []
        pauses.hold(conn, "switch:intake", "x")
        pauses.hold(conn, "switch:publish", "x")
        pauses.start(conn, "llm:something", 600, "not a switch")
        assert pauses.switches_on(conn) == ["intake", "publish"]


# ---------- live and ended: pausable apart, `intake` is both ----------
def test_a_new_live_recording_is_stopped_by_live_or_intake_and_not_by_ended(db):
    for held, live_paused, ended_paused in (
        ("live", True, False), ("ended", False, True), ("intake", True, True), ("publish", False, False),
    ):  # fmt: skip
        with db.begin() as conn:
            pauses.hold(conn, pauses.switch_key(held), "t")
            assert pauses.intake_paused(conn, "live") is live_paused
            assert pauses.intake_paused(conn, "ended") is ended_paused
            pauses.release(conn, pauses.switch_key(held))


def test_nothing_is_paused_until_a_switch_is_held(db):
    with db.connect() as conn:
        assert not pauses.intake_paused(conn, "live") and not pauses.intake_paused(conn, "ended")


def test_the_switches_you_can_pause_by_hand():
    assert set(pauses.SWITCHES) == {"publish", "live", "ended", "intake"}
