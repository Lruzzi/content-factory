"""The live-watcher: which sources get checked, and what a live stream turns into (FR-15, FR-10)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import sources
from services.live_watcher import detect, watcher

pytestmark = pytest.mark.integration

CONFIG = cs.ROOT
CAM = {"layout": {"cam": {"x": 1400, "y": 700, "w": 500, "h": 340}}}
LEO = "https://www.youtube.com/@leokabuki"
JEJE = "https://www.youtube.com/@JejeAdriel20"
RINE = "https://www.youtube.com/@Rineeeloyyyeh"
OTHER = "https://www.youtube.com/@newcreator"


def live(video_id="vid00001"):
    return detect.Detection("live", video_id, f"https://www.youtube.com/watch?v={video_id}", "t", "")


class Checks:
    """A stand-in for yt-dlp: answers per channel url and remembers what was asked."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def __call__(self, url):
        self.asked.append(url)
        answer = self.answers.get(url)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def seeded(db):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, CONFIG)


def add(db, url=OTHER, **kw):
    with db.begin() as conn:
        return sources.add_source(
            conn, CONFIG, url=url, profile="mlbb", overrides=kw.pop("overrides", CAM), updated_by="t", **kw
        )


def tick(db, check):
    with db.begin() as conn:
        return watcher.tick(conn, CONFIG, check)


def streams(db):
    with db.connect() as conn:
        return conn.execute(sa.text("SELECT * FROM live_streams")).mappings().all()


def test_a_live_stream_becomes_a_row_linked_to_its_source(db, seeded):
    result = tick(db, Checks({LEO: live()}))
    assert result.started == ["vid00001"]
    (stream,) = streams(db)
    assert stream["video_id"] == "vid00001" and stream["status"] == "live" and stream["lease_worker"] is None
    with db.connect() as conn:
        key = conn.execute(
            sa.text("SELECT source_key FROM sources WHERE id = :i"), {"i": stream["source_id"]}
        ).scalar_one()
    assert key == "leokabuki"


def test_the_same_stream_is_not_started_twice(db, seeded):
    check = Checks({LEO: live()})
    tick(db, check)
    assert tick(db, check).started == []
    assert len(streams(db)) == 1


def test_a_stream_that_already_ended_is_not_brought_back(db, seeded):
    check = Checks({LEO: live()})
    tick(db, check)
    with db.begin() as conn:
        conn.execute(sa.text("UPDATE live_streams SET status = 'ended', ended_at = clock_timestamp()"))
    assert tick(db, check).started == []
    assert streams(db)[0]["status"] == "ended"


def test_upcoming_and_not_live_sources_start_nothing(db, seeded):
    upcoming = detect.Detection("upcoming", "vid00002", "u", "t", "")
    assert tick(db, Checks({LEO: upcoming})).started == []
    assert tick(db, Checks({LEO: None})).started == []
    assert streams(db) == []


def test_a_members_only_stream_is_skipped_and_reported(db, seeded):
    members = detect.Detection("skipped", "vid00003", "u", "t", "subscriber_only")
    result = tick(db, Checks({LEO: members}))
    assert result.started == [] and streams(db) == []
    assert result.skipped == ["vid00003: subscriber_only"]


def test_only_sources_that_may_be_recorded_are_checked(db, seeded):
    paused = add(db, "https://www.youtube.com/@paused1")
    add(db, "https://www.youtube.com/@blocked1", clip_policy="blocked")
    add(db, "https://www.youtube.com/@nocapture", overrides=CAM | {"live": {"capture": False}})
    with db.begin() as conn:
        sources.pause(conn, paused["id"], updated_by="t")
    check = Checks({})
    tick(db, check)
    assert sorted(check.asked) == sorted([LEO, JEJE, RINE])  # the three creators the YAML seeds


def test_one_failing_source_does_not_stop_the_others(db, seeded):
    add(db)
    check = Checks({LEO: detect.DetectError("HTTP Error 429"), OTHER: live("vid00004")})
    result = tick(db, check)
    assert result.started == ["vid00004"]
    assert len(result.problems) == 1 and "leokabuki" in result.problems[0] and "429" in result.problems[0]


def test_a_source_row_that_fails_validation_is_reported_not_fatal(db, seeded):
    with db.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO sources (profile, source_key, url) VALUES ('mlbb', 'broken', 'https://example.com/x')")
        )
    result = tick(db, Checks({LEO: live()}))
    assert result.started == ["vid00001"]
    assert any("broken" in p for p in result.problems)


def test_the_next_check_waits_for_the_shortest_interval_of_the_sources_checked(db, seeded):
    add(db, overrides=CAM | {"live": {"check_every_min": 2}})
    assert tick(db, Checks({})).wait_min == 2


def test_no_new_stream_is_started_while_intake_is_paused(db, seeded):
    from common import pauses

    with db.begin() as conn:
        pauses.hold(conn, "switch:intake", "paused from Telegram")
    check = Checks({LEO: live("vid00001")})
    result = tick(db, check)
    assert check.asked == [] and result.started == []
    with db.begin() as conn:
        pauses.release(conn, "switch:intake")
    assert tick(db, check).started == ["vid00001"]


def test_only_the_live_switch_stops_new_streams_not_the_ended_one(db, seeded):
    from common import pauses

    with db.begin() as conn:
        pauses.hold(conn, "switch:ended", "paused from Telegram")  # ended streams: nothing to do with live ones
    check = Checks({LEO: live("vid00001")})
    assert tick(db, check).started == ["vid00001"]
    with db.begin() as conn:
        pauses.release(conn, "switch:ended")
        pauses.hold(conn, "switch:live", "paused from Telegram")
    check2 = Checks({LEO: live("vid00002")})
    assert tick(db, check2).started == [] and check2.asked == []
