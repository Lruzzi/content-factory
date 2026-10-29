"""Chat spikes: where the live chat came in far faster than usual, on the stream's own clock."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

import config_schema as cs
from services.select import chatspikes as cs_

RULES = cs.ChatSignal(
    enabled=True, weight=0.35, window_s=10, min_messages=5, min_ratio=3.0, lag_s=12, before_s=20, after_s=8
)
WINDOW = (100.0, 700.0)


def detect(counts, usual=0.8, rules=RULES, window=WINDOW):
    return cs_.detect(counts, bucket_s=10, usual=usual, rules=rules, window=window)


# ---------- finding the spikes ----------
def test_a_burst_well_over_the_usual_rate_is_a_spike():
    (spike,) = detect({300.0: 2, 310.0: 4, 320.0: 1})
    assert (spike.start_s, spike.end_s, spike.messages) == (300.0, 330.0, 7)


def test_a_chat_at_its_usual_rate_has_no_spike_however_many_messages_it_has_in_all():
    assert detect({t: 1 for t in range((100.0).__int__(), 700, 10)}) == []


def test_it_needs_both_enough_messages_and_enough_times_the_usual_rate():
    assert detect({300.0: 2, 310.0: 2}) == []  # 4 messages: under min_messages
    assert detect({300.0: 4, 310.0: 4}, usual=3.0) == []  # 8 messages, but a lively chat has 18 in two buckets anyway


def test_a_very_quiet_chat_is_judged_against_a_floor_so_a_couple_of_messages_is_no_spike():
    assert detect({300.0: 3, 310.0: 1}, usual=0.0) == []  # 4 < 5
    assert len(detect({300.0: 3, 310.0: 2}, usual=0.0)) == 1


def test_stretches_that_touch_are_one_spike_and_far_apart_ones_are_two():
    spikes = detect({300.0: 5, 310.0: 5, 320.0: 5, 500.0: 6, 510.0: 1})
    assert [(s.start_s, s.messages) for s in spikes] == [(300.0, 15), (500.0, 7)]


def test_only_a_spike_that_starts_inside_the_segment_counts():
    assert detect({50.0: 9, 60.0: 9}) == [] and detect({750.0: 9, 760.0: 9}) == []


def test_at_most_max_per_segment_are_kept_the_biggest_first_then_in_time_order():
    rules = RULES.model_copy(update={"max_per_segment": 2})
    counts = {200.0: 6, 300.0: 9, 400.0: 12, 500.0: 7}
    assert [s.start_s for s in detect(counts, rules=rules)] == [300.0, 400.0]


# ---------- showing them to the judge ----------
def rows(*spans):
    return [{"text": f"kata {i}", "start_s": a, "end_s": b} for i, (a, b) in enumerate(spans)]


def test_a_sentence_spoken_during_a_spike_gets_a_note_and_one_outside_it_does_not():
    out = cs_.mark(rows((100, 110), (305, 315), (400, 410)), [cs_.Spike(300, 330, 9)], RULES, WINDOW)
    assert out[1]["text"].startswith("[chat spike: 9 messages in 30 s] kata 1")
    assert out[0]["text"] == "kata 0" and out[2]["text"] == "kata 2"


def test_a_spike_nobody_spoke_in_becomes_a_line_of_its_own_as_long_as_the_clip_would_be():
    out = cs_.mark(rows((100, 110), (400, 410)), [cs_.Spike(300, 330, 9)], RULES, WINDOW)
    assert [r["start_s"] for r in out] == [100, 280.0, 400]
    assert out[1]["end_s"] == 338.0 and "nobody spoke" in out[1]["text"]


def test_the_line_of_its_own_stays_inside_the_segment():
    out = cs_.mark([], [cs_.Spike(105, 135, 9), cs_.Spike(690, 700, 9)], RULES, WINDOW)
    assert (out[0]["start_s"], out[1]["end_s"]) == (100.0, 700.0)


# ---------- from the database ----------
pytestmark_db = pytest.mark.integration


def stream_with_segment(db, *, created_s_ago=0.0):
    """A stream whose segment 4 (stream clock 100-700 s) was stored `created_s_ago` seconds ago."""
    with db.begin() as conn:
        stream = conn.execute(
            sa.text("INSERT INTO live_streams (video_id) VALUES ('vidspike001') RETURNING id")
        ).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO segments (stream_id, idx, key, start_s, end_s, created_at) VALUES "
                "(:s, 4, 'k', 100, 700, clock_timestamp() - make_interval(secs => :a))"
            ),
            {"s": stream, "a": created_s_ago},
        )
        wall_end = float(conn.execute(sa.text("SELECT extract(epoch FROM created_at) FROM segments")).scalar_one())
    return stream, wall_end


def put(db, stream, wall_epoch, n):
    with db.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO chat_counts (stream_id, bucket_at, n) VALUES (:s, to_timestamp(:t), :n)"),
            {"s": stream, "t": wall_epoch, "n": n},
        )


@pytest.mark.integration
def test_a_burst_of_chat_in_the_segment_becomes_a_spike_on_the_stream_clock_less_the_reaction_time(db):
    stream, wall_end = stream_with_segment(db)
    wall_start = wall_end - 600
    for i in range(60):  # a steady trickle: about one message a bucket
        put(db, stream, wall_start + 10 * i, 1)
    put(db, stream, wall_start + 300 + 12, 8)  # a burst 312 s into the segment: the event was at 300 s
    put(db, stream, wall_start + 310 + 12, 6)
    with db.connect() as conn:
        spikes = cs_.load(conn, stream, 4, RULES)
    assert len(spikes) == 1
    assert spikes[0].start_s == pytest.approx(100 + 300, abs=10) and spikes[0].messages >= 14


@pytest.mark.integration
def test_a_segment_whose_chat_was_not_listened_to_has_no_spikes(db):
    stream, _ = stream_with_segment(db)
    with db.connect() as conn:
        assert cs_.load(conn, stream, 4, RULES) == [] and cs_.load(conn, stream, 99, RULES) == []
