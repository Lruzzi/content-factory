"""One round of discover: for each source that is not live, pick its best ended stream or upload and queue a download.

A source is left alone when it is paused or blocked (a claim pauses it, FR-10), when it is live right now (the recorder
has it), when an LLM quota is used up (nothing fetched now could become clips), when Groq's audio for the day is
already half used or too much is waiting (live capture comes first), and once it has had its turn for the day. Only
public videos are ever picked; members-only and private ones never are."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa

from common import jobs, pauses, sources
from services.discover import rules
from services.discover import youtube as yt
from services.understand import budget

log = logging.getLogger(__name__)
MAX_LOOKUPS = 12  # video pages read per source and round, at most


@dataclass
class TickResult:
    queued: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _count(conn: sa.Connection, sql: str, **params: object) -> int:
    return int(conn.execute(sa.text(sql), params).scalar_one())


def tick(
    engine: sa.Engine,
    config_dir: Path,
    *,
    lister: Callable[[str], list[yt.Video]] = yt.list_videos,
    detail: Callable[[str], yt.Video] = yt.video_info,
    limits: budget.Limits,
    publish_job: Callable[[uuid.UUID], object] | None = None,
    now: datetime | None = None,
) -> TickResult:
    now = now or datetime.now(UTC)
    result = TickResult()
    new_jobs: list[tuple[uuid.UUID, str]] = []
    with engine.connect() as conn:
        if pauses.intake_paused(conn, "ended"):
            result.skipped.append("discovery of ended streams is paused by hand (/resume in Telegram)")
            return result
        profiles, result.problems = sources.load_profiles(conn, config_dir)
        stored = {(r["profile"], r["source_key"]): r for r in sources.list_sources(conn)}
        known = set(conn.execute(sa.text("SELECT video_id FROM live_streams")).scalars())
        backlog = _count(
            conn,
            "SELECT count(*) FROM jobs WHERE stage IN ('download', 'understand', 'select') "
            "AND (status IN ('queued', 'running', 'waiting') OR (status = 'failed' AND retry_at IS NOT NULL))",
        )
        share = budget.day_share(conn, limits)
        quota_left = conn.execute(
            sa.text(
                "SELECT min(until) - clock_timestamp() FROM pauses WHERE key LIKE 'llm:%' AND until > clock_timestamp()"
            )
        ).scalar_one()
    for profile in profiles:
        row = stored[(profile.name, profile.source.id)]
        d = profile.discover
        if not (profile.enabled and profile.source.enabled) or profile.source.clip_policy == "blocked":
            continue
        if row["paused_at"] is not None or (d.backfill_per_source == 0 and d.daily_per_source == 0):
            continue
        label = f"{profile.name}/{profile.source.id}"
        if quota_left is not None:  # nothing fetched now could be turned into clips: the LLM is out of quota
            result.skipped.append(
                f"{label}: the LLM quota is used up (back in about {quota_left.total_seconds() / 60:.0f} min)"
            )
            continue
        if share >= d.max_groq_day_share:
            result.skipped.append(
                f"{label}: Groq audio is {share:.0%} used today (the limit here is {d.max_groq_day_share:.0%})"
            )
            continue
        if backlog > d.max_backlog:
            result.skipped.append(f"{label}: {backlog} jobs are waiting already (the limit here is {d.max_backlog})")
            continue
        with engine.connect() as conn:
            live_now = _count(
                conn, "SELECT count(*) FROM live_streams WHERE source_id = :s AND status = 'live'", s=row["id"]
            )
            ever = _count(conn, "SELECT count(*) FROM jobs WHERE source_id = :s AND stage = 'download'", s=row["id"])
            last_day = _count(
                conn,
                "SELECT count(*) FROM jobs WHERE source_id = :s AND stage = 'download' "
                "AND created_at > clock_timestamp() - interval '24 hours'",
                s=row["id"],
            )
        if live_now:
            continue
        allowed = rules.allowance(d, ever=ever, last_day=last_day)
        if allowed <= 0:
            continue
        try:
            listed = lister(profile.source.url)
        except yt.DiscoverError as exc:
            result.problems.append(f"{label}: {exc}")
            continue
        wanted: list[yt.Video] = []
        for video in listed:
            reason = rules.skip_reason(video, d, known=known)
            if reason is None:
                wanted.append(video)
            elif "already" not in reason:
                result.skipped.append(f"{video.id}: {reason}")
        # the newest ones first (the list is newest first), then the most viewed: enough to find the best few
        looked = (wanted[:3] + sorted(wanted[3:], key=lambda v: v.views, reverse=True))[:MAX_LOOKUPS]
        usable: list[yt.Video] = []
        for video in looked:
            try:
                full = detail(video.id)
            except yt.DiscoverError as exc:
                result.problems.append(f"{label}: {video.id}: {exc}")
                continue
            reason = rules.detail_skip_reason(full, d, now)
            if reason is None:
                usable.append(full)
            else:
                result.skipped.append(f"{video.id}: {reason}")
        for video in rules.rank(usable, now)[:allowed]:
            start, length = rules.best_window(video.heatmap, video.duration_s or 0.0, d.max_window_min * 60)
            stream_id = uuid.uuid4()
            with engine.begin() as conn:
                opened = conn.execute(
                    sa.text(
                        "INSERT INTO live_streams (id, source_id, video_id, status, ended_at) "
                        "VALUES (:i, :s, :v, 'ended', clock_timestamp()) "
                        "ON CONFLICT (tenant_id, video_id) DO NOTHING RETURNING id"
                    ),
                    {"i": stream_id, "s": row["id"], "v": video.id},
                ).first()
                if opened is None:
                    continue
                job_id = jobs.create_job(
                    conn, profile, stage="download", source_id=row["id"],
                    data={
                        "video_id": video.id, "stream_id": str(stream_id), "title": video.title,
                        "start_s": start, "length_s": length, "duration_s": video.duration_s,
                    },
                )  # fmt: skip
            known.add(video.id)
            result.queued.append(video.id)
            new_jobs.append((job_id, video.id))
            backlog += 1
    for job_id, video_id in new_jobs:
        if publish_job is not None:
            try:
                publish_job(job_id)
            except Exception:
                log.exception("could not queue download %s of %s; it stays queued for the reconciler", job_id, video_id)
    return result
