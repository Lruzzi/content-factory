"""Delete what is no longer needed, so the disk never fills up. Recordings are the big ones (about 2 GB an hour).

Source recordings: kept while a job still needs them (not yet transcribed or selected, or a clip not yet rendered from
them), and for at least the profile's `live.max_buffer_h`. At the `live.max_hold_h` ceiling a recording goes even if a
job is stuck, and you are told which. Clips: a rejected or expired one goes at once; an approved one stays until it is
published everywhere (then `published_keep_h` more); one with a failed post stays `failed_keep_days` so it can be
recovered; one approved but still unpublished after `approved_max_hold_days` is deleted, with an alert.

Only files under `segments/` and `clips/` are ever deleted, and every deletion is logged with its reason."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass

import sqlalchemy as sa

from common.media import LocalStore, MediaKeyError

log = logging.getLogger(__name__)
DELETABLE = re.compile(r"(segments|clips)/[A-Za-z0-9_][A-Za-z0-9._/-]*")
DEFAULT_BUFFER_H, DEFAULT_HOLD_H = 3.0, 12.0


@dataclass(frozen=True)
class Policy:
    published_keep_h: float
    failed_keep_days: float
    approved_max_hold_days: float


@dataclass(frozen=True)
class Swept:
    segments: int = 0
    clips: int = 0
    bytes: int = 0


def _remove(store: LocalStore, key: str, why: str) -> int | None:
    """Delete one file. Returns the bytes freed (0 if it was already gone), or None if the key is not ours to delete."""
    if not DELETABLE.fullmatch(key) or ".." in key:
        log.warning("not deleting %r (%s): it is not a recording or clip path", key, why)
        return None
    try:
        path = store.path(key)
    except MediaKeyError:
        log.warning("not deleting %r (%s): not a valid media key", key, why)
        return None
    size = path.stat().st_size if path.is_file() else 0
    store.delete(key)
    log.info("deleted %s (%s)", key, why)
    return size


def _segments(engine: sa.Engine, store: LocalStore, alert: Callable[[str], object]) -> tuple[int, int]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                sa.text(
                    "SELECT s.id, s.key, EXTRACT(EPOCH FROM clock_timestamp() - s.created_at) / 3600 AS age_h, "
                    "COALESCE((j.spec->'live'->>'max_buffer_h')::float, :buf) AS buf_h, "
                    "COALESCE((j.spec->'live'->>'max_hold_h')::float, :hold) AS hold_h, "
                    "ARRAY(SELECT n.id::text FROM jobs n WHERE n.status NOT IN ('done', 'dead') AND ("
                    "(n.media_key = s.key AND n.stage IN ('understand', 'select')) "
                    "OR (n.stage = 'render' AND (n.data->'render'->>'source_key' = s.key "
                    "OR n.data->'render'->'pieces' @> jsonb_build_array(jsonb_build_object('source_key', s.key))))"
                    ")) AS waiting "
                    "FROM segments s LEFT JOIN LATERAL "
                    "(SELECT spec FROM jobs WHERE media_key = s.key ORDER BY created_at LIMIT 1) j ON true "
                    "WHERE s.deleted_at IS NULL"
                ),
                {"buf": DEFAULT_BUFFER_H, "hold": DEFAULT_HOLD_H},
            )
            .mappings()
            .all()
        )
    count = freed = 0
    for row in rows:
        if row["age_h"] < row["buf_h"] or (row["waiting"] and row["age_h"] < row["hold_h"]):
            continue
        why = "older than the buffer, nothing needs it"
        if row["waiting"]:
            why = "past the hold ceiling"
            alert(
                f"the recording {row['key']} was deleted at the {row['hold_h']:g} h ceiling while "
                f"{len(row['waiting'])} job(s) still needed it: {', '.join(row['waiting'][:3])}"
            )
        size = _remove(store, row["key"], why)
        if size is None:
            continue
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE segments SET deleted_at = clock_timestamp() WHERE id = :i"), {"i": row["id"]})
        count, freed = count + 1, freed + size
    return count, freed


def _clips(engine: sa.Engine, store: LocalStore, policy: Policy, alert: Callable[[str], object]) -> tuple[int, int]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT key, reason FROM ("
                "SELECT data->'render'->'output'->>'key' AS key, "
                "EXTRACT(EPOCH FROM clock_timestamp() - updated_at) / 3600 AS age_h, stage, status, "
                "data->'review'->>'state' AS review, "
                "EXISTS (SELECT 1 FROM posts p WHERE p.job_id = jobs.id AND p.state = 'failed') AS failed "
                "FROM jobs WHERE data->'render'->'output'->>'key' IS NOT NULL "
                "AND updated_at > clock_timestamp() - interval '60 days') j, "
                "LATERAL (SELECT CASE "
                "WHEN review IN ('rejected', 'expired') THEN 'rejected or expired' "
                "WHEN stage = 'publish' AND status = 'done' AND failed AND age_h > :failed_h "
                "THEN 'a post failed; kept long enough' "
                "WHEN stage = 'publish' AND status = 'done' AND NOT failed AND age_h > :keep_h "
                "THEN 'published everywhere' "
                "WHEN stage = 'publish' AND status = 'queued' AND age_h > :hold_h THEN 'not published' "
                "END AS reason) r WHERE reason IS NOT NULL"
            ),
            {
                "failed_h": policy.failed_keep_days * 24,
                "keep_h": policy.published_keep_h,
                "hold_h": policy.approved_max_hold_days * 24,
            },
        ).all()
    count = freed = 0
    for key, why in rows:
        if not store_has(store, key):
            continue
        size = _remove(store, key, why)
        if size is None:
            continue
        if why == "not published":
            alert(
                f"the clip {key} was approved over {policy.approved_max_hold_days:g} days ago "
                "but is not published yet: deleted it"
            )
        count, freed = count + 1, freed + size
    return count, freed


def store_has(store: LocalStore, key: str) -> bool:
    try:
        return store.path(key).is_file()
    except MediaKeyError:
        return True  # let _remove report it


def sweep(engine: sa.Engine, store: LocalStore, policy: Policy, alert: Callable[[str], object] | None = None) -> Swept:
    alert = alert or (lambda message: None)
    segments, freed_segments = _segments(engine, store, alert)
    clips, freed_clips = _clips(engine, store, policy, alert)
    return Swept(segments, clips, freed_segments + freed_clips)


def run(
    engine: sa.Engine,
    store: LocalStore,
    policy: Policy,
    *,
    every_s: float,
    stop: threading.Event,
    alert: Callable[[str], object] | None = None,
) -> None:
    """Sweep on a timer. A failed sweep is reported and tried again; it never ends the loop."""
    while not stop.is_set():
        try:
            swept = sweep(engine, store, policy, alert)
            if swept.segments or swept.clips:
                log.info("freed %.1f MB: %d recordings, %d clips", swept.bytes / 1e6, swept.segments, swept.clips)
        except Exception as exc:
            log.exception("a retention sweep failed; trying again at the next tick")
            if alert:
                alert(f"the disk clean-up could not run: {type(exc).__name__}: {exc}")
        stop.wait(every_s)
