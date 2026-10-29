"""Moments where the live chat came in much faster than usual. The chat listener keeps only how many messages came in
each bucket of wall-clock time; this turns the buckets of one segment into stretches on the stream's own clock, and
puts them in front of the judge (a marker on the sentences spoken then, or a line of their own where nobody spoke)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

import config_schema as cs

MIN_RATE = 0.2  # messages per bucket: a chat quieter than this is judged against this, so a single message is no spike
HISTORY_S = 1800  # how far back "the usual rate" looks


@dataclass(frozen=True)
class Spike:
    start_s: float  # on the stream clock: when it happened (the burst, less the time people need to react)
    end_s: float
    messages: int


def detect(
    counts: Mapping[float, int], *, bucket_s: int, usual: float, rules: cs.ChatSignal, window: tuple[float, float]
) -> list[Spike]:
    """`counts` maps the stream-clock start of each bucket to its messages; `usual` is the normal messages per bucket.
    A stretch of two buckets counts when it has at least `min_messages` and `min_ratio` times the usual; stretches
    that touch are one spike. Only spikes that start inside `window` (the segment) are returned, most messages first,
    at most `max_per_segment`, then in time order."""
    expected = max(usual, MIN_RATE) * 2 * rules.min_ratio
    hot: list[tuple[float, float]] = []
    for start in sorted(counts):
        n = counts[start] + counts.get(start + bucket_s, 0)
        if n >= rules.min_messages and n >= expected:
            hot.append((start, start + 2 * bucket_s))
    merged: list[list[float]] = []
    for a, b in hot:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    spikes = [
        Spike(a, b, sum(n for t, n in counts.items() if a <= t < b)) for a, b in merged if window[0] <= a < window[1]
    ]
    best = sorted(spikes, key=lambda s: -s.messages)[: rules.max_per_segment]
    return sorted(best, key=lambda s: s.start_s)


def load(conn: sa.Connection, stream_id: uuid.UUID, segment: int, rules: cs.ChatSignal) -> list[Spike]:
    """The chat spikes of one recorded segment, or none when its chat was not listened to."""
    bucket_s = rules.window_s or 10
    seg = conn.execute(
        sa.text(
            "SELECT extract(epoch FROM created_at), start_s, end_s FROM segments WHERE stream_id = :s AND idx = :i"
        ),
        {"s": stream_id, "i": segment},
    ).first()
    if seg is None:
        return []
    wall_end, start_s, end_s = float(seg[0]), float(seg[1]), float(seg[2])
    wall_start = wall_end - (end_s - start_s)  # the segment is stored when it is finished
    rows = conn.execute(
        sa.text(
            "SELECT extract(epoch FROM bucket_at), n FROM chat_counts WHERE stream_id = :s "
            "AND bucket_at >= to_timestamp(:a) AND bucket_at < to_timestamp(:b)"
        ),
        {"s": stream_id, "a": wall_start + rules.lag_s, "b": wall_end + rules.lag_s + 2 * bucket_s},
    ).all()
    usual = conn.execute(
        sa.text(
            "SELECT sum(n), extract(epoch FROM max(bucket_at) - min(bucket_at)) FROM chat_counts "
            "WHERE stream_id = :s AND bucket_at >= to_timestamp(:a) AND bucket_at < to_timestamp(:b)"
        ),
        {"s": stream_id, "a": wall_end - HISTORY_S, "b": wall_end + rules.lag_s + 2 * bucket_s},
    ).one()
    if not rows or usual[0] is None:
        return []
    per_bucket = float(usual[0]) / (float(usual[1] or 0) / bucket_s + 1)
    counts = {start_s + float(t) - rules.lag_s - wall_start: int(n) for t, n in rows}
    return detect(counts, bucket_s=bucket_s, usual=per_bucket, rules=rules, window=(start_s, end_s))


def mark(
    rows: list[dict[str, Any]], spikes: list[Spike], rules: cs.ChatSignal, window: tuple[float, float]
) -> list[dict[str, Any]]:
    """The sentences (dicts with text, start_s, end_s) with the chat spikes in them: a sentence spoken during a spike
    gets a note in front of it; a spike nobody spoke in gets a line of its own, as long as the clip would be."""
    out = [dict(r) for r in rows]
    for spike in spikes:
        note = f"[chat spike: {spike.messages} messages in {spike.end_s - spike.start_s:.0f} s] "
        hit = [r for r in out if r["start_s"] < spike.end_s and r["end_s"] > spike.start_s]
        if hit:
            for r in hit:
                if not r["text"].startswith("[chat spike"):
                    r["text"] = note + r["text"]
        else:
            out.append(
                {
                    "text": note + "(nobody spoke; the audience reacted to what was on screen)",
                    "start_s": max(window[0], spike.start_s - rules.before_s),
                    "end_s": min(window[1], spike.end_s + rules.after_s),
                }
            )
    return sorted(out, key=lambda r: (r["start_s"], r["end_s"]))
