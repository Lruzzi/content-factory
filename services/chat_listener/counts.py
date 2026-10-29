"""Chat messages counted per bucket of wall-clock time (10 s by default), kept in `chat_counts`. Only counts: see
reader.py."""

from __future__ import annotations

import math
import uuid
from collections import Counter

import sqlalchemy as sa


def bucket_of(epoch_s: float, bucket_s: int) -> int:
    """The start of the bucket a time falls in, in whole seconds since the epoch."""
    return math.floor(epoch_s / bucket_s) * bucket_s


def add(conn: sa.Connection, stream_id: uuid.UUID, counted: Counter[int]) -> None:
    """Add these counts (bucket start -> messages) to the stored ones: a bucket flushed twice adds up."""
    for start, n in counted.items():
        conn.execute(
            sa.text(
                "INSERT INTO chat_counts (stream_id, bucket_at, n) VALUES (:s, to_timestamp(:t), :n) "
                "ON CONFLICT (tenant_id, stream_id, bucket_at) DO UPDATE SET n = chat_counts.n + EXCLUDED.n"
            ),
            {"s": stream_id, "t": start, "n": n},
        )
