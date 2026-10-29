"""One pass of the live-watcher: check every source that may be recorded and open a `live_streams` row for each
stream that is live. The recorder picks the rows up (leases.needing_a_worker)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa

from common import pauses, sources
from services.live_watcher import detect


@dataclass
class TickResult:
    started: list[str] = field(default_factory=list)  # video ids of new live streams
    skipped: list[str] = field(default_factory=list)  # "<video id>: <why>" for streams never recorded
    problems: list[str] = field(default_factory=list)
    wait_min: int = 4  # until the next pass: the shortest check_every_min of the sources checked


def tick(
    conn: sa.Connection, config_dir: Path, check: Callable[[str], detect.Detection | None] = detect.check
) -> TickResult:
    result = TickResult()
    if pauses.intake_paused(conn, "live"):
        return result  # new live recordings are paused by hand: no new stream is looked for or started
    profiles, result.problems = sources.load_profiles(conn, config_dir)
    checked = False
    rows = {(r["profile"], r["source_key"]): r for r in sources.list_sources(conn)}
    for profile in profiles:
        row = rows[(profile.name, profile.source.id)]
        # a paused source (copyright claim, FR-10) and a blocked one are not recorded at all
        if not (profile.enabled and profile.live.capture and profile.source.enabled):
            continue
        if profile.source.clip_policy == "blocked" or row["paused_at"] is not None:
            continue
        result.wait_min = (
            min(result.wait_min, profile.live.check_every_min) if checked else profile.live.check_every_min
        )
        checked = True
        try:
            found = check(profile.source.url)
        except detect.DetectError as exc:
            result.problems.append(f"{profile.name}/{profile.source.id}: {exc}")
            continue
        if found is None or found.state == "upcoming":
            continue
        if found.state == "skipped":
            result.skipped.append(f"{found.video_id}: {found.reason}")
            continue
        opened = conn.execute(
            sa.text(
                "INSERT INTO live_streams (source_id, video_id) VALUES (:source, :video) "
                "ON CONFLICT (tenant_id, video_id) DO NOTHING RETURNING id"
            ),
            {"source": row["id"], "video": found.video_id},
        ).first()
        if opened is not None:
            result.started.append(found.video_id)
    return result
