"""Table definitions (SQLAlchemy Core).

The Alembic migrations are written by hand; a test checks that they and these definitions agree.
Every data table has `tenant_id` (one default tenant in version 1), see TECH.md Scale-readiness.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
KINDS = ("highlight", "donation", "tutorial", "news", "funny")

metadata = MetaData()


def _id() -> Column:
    return Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))


def _tenant() -> Column:
    return Column(
        "tenant_id",
        UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        server_default=text(f"'{DEFAULT_TENANT_ID}'::uuid"),
    )


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=text("now()"))


tenants = Table("tenants", metadata, _id(), Column("name", Text, nullable=False, unique=True), _stamp("created_at"))

sources = Table(
    "sources",
    metadata,
    _id(),
    _tenant(),
    Column("profile", Text, nullable=False),
    Column("source_key", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("name", Text),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("clip_policy", Text, nullable=False, server_default=text("'open'")),
    Column("overrides", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("paused_at", DateTime(timezone=True)),
    Column("pause_reason", Text),
    Column("claim_count", Integer, nullable=False, server_default=text("0")),
    Column("last_polled_at", DateTime(timezone=True)),
    _stamp("created_at"),
    _stamp("updated_at"),
    Column("updated_by", Text),
    CheckConstraint("clip_policy IN ('open', 'blocked')", name="ck_sources_clip_policy"),
    CheckConstraint("pause_reason IN ('manual', 'claim')", name="ck_sources_pause_reason"),
    CheckConstraint("(paused_at IS NULL) = (pause_reason IS NULL)", name="ck_sources_pause_pair"),
    UniqueConstraint("tenant_id", "profile", "source_key", name="uq_sources_profile_key"),
)

jobs = Table(
    "jobs",
    metadata,
    _id(),
    _tenant(),
    Column("source_id", UUID(as_uuid=True), ForeignKey("sources.id")),
    Column("stage", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'queued'")),
    Column("kind", Text),
    Column("spec", JSONB, nullable=False),  # the resolved profile, taken when the job was created
    Column("data", JSONB, nullable=False, server_default=text("'{}'::jsonb")),  # what each stage hands to the next
    Column("media_key", Text),
    Column("attempt", Integer, nullable=False, server_default=text("0")),
    Column("retry_at", DateTime(timezone=True)),
    Column("last_error", Text),
    _stamp("created_at"),
    _stamp("updated_at"),
    CheckConstraint("status IN ('queued', 'running', 'waiting', 'done', 'failed', 'dead')", name="ck_jobs_status"),
    CheckConstraint("attempt >= 0", name="ck_jobs_attempt"),
    CheckConstraint(
        "kind IS NULL OR kind IN ('highlight', 'donation', 'tutorial', 'news', 'funny', 'recap')", name="ck_jobs_kind"
    ),
    Index("ix_jobs_status_retry_at", "status", "retry_at"),
)

live_streams = Table(
    "live_streams",
    metadata,
    _id(),
    _tenant(),
    Column("source_id", UUID(as_uuid=True), ForeignKey("sources.id")),
    Column("video_id", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'live'")),
    _stamp("started_at"),
    Column("ended_at", DateTime(timezone=True)),
    Column("lease_worker", Text),
    Column("lease_heartbeat_at", DateTime(timezone=True)),
    Column("recap_checked_s", Float),  # how far into the stream the match finder last looked: see recap_step.py
    Column("chat_worker", Text),  # the chat listener's own lease (the recorder holds lease_worker)
    Column("chat_heartbeat_at", DateTime(timezone=True)),
    CheckConstraint("status IN ('live', 'ended')", name="ck_live_streams_status"),
    CheckConstraint("(lease_worker IS NULL) = (lease_heartbeat_at IS NULL)", name="ck_live_streams_lease_pair"),
    CheckConstraint("(chat_worker IS NULL) = (chat_heartbeat_at IS NULL)", name="ck_live_streams_chat_lease_pair"),
    UniqueConstraint("tenant_id", "video_id", name="uq_live_streams_video"),
)

chat_counts = Table(  # how many chat messages came in each 10 s of a live stream: see services/chat_listener
    "chat_counts",
    metadata,
    _tenant(),
    Column("stream_id", UUID(as_uuid=True), ForeignKey("live_streams.id", ondelete="CASCADE"), nullable=False),
    Column("bucket_at", DateTime(timezone=True), nullable=False),  # the wall-clock start of the bucket
    Column("n", Integer, nullable=False),
    PrimaryKeyConstraint("tenant_id", "stream_id", "bucket_at", name="pk_chat_counts"),
    CheckConstraint("n >= 0", name="ck_chat_counts_n"),
)

segments = Table(
    "segments",
    metadata,
    _id(),
    _tenant(),
    Column("stream_id", UUID(as_uuid=True), ForeignKey("live_streams.id", ondelete="CASCADE"), nullable=False),
    Column("idx", Integer, nullable=False),
    Column("key", Text, nullable=False),
    Column("start_s", Numeric(12, 3), nullable=False),  # seconds since the recording began
    Column("end_s", Numeric(12, 3), nullable=False),
    _stamp("created_at"),
    Column("deleted_at", DateTime(timezone=True)),  # when the recording file was removed to free disk
    CheckConstraint("idx >= 0 AND start_s >= 0 AND end_s >= start_s", name="ck_segments_range"),
    UniqueConstraint("stream_id", "idx", name="uq_segments_stream_idx"),
)

transcripts = Table(
    "transcripts",
    metadata,
    _id(),
    _tenant(),
    Column("job_id", UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL")),
    Column("stream_id", UUID(as_uuid=True), ForeignKey("live_streams.id", ondelete="CASCADE")),
    Column("media_key", Text, nullable=False),
    Column("start_s", Numeric(12, 3), nullable=False),  # where this media starts on the stream clock
    Column("duration_s", Numeric(12, 3), nullable=False),
    Column("language", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("sentences", JSONB, nullable=False),  # [{text, start_s, end_s}] on the stream clock
    Column("words", JSONB, nullable=False),
    _stamp("created_at"),
    CheckConstraint("start_s >= 0 AND duration_s >= 0", name="ck_transcripts_range"),
    UniqueConstraint("tenant_id", "media_key", name="uq_transcripts_media"),
    Index("ix_transcripts_stream_start", "stream_id", "start_s"),
)

posts = Table(
    "posts",
    metadata,
    _id(),
    _tenant(),
    Column("job_id", UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
    Column("account_id", Text, nullable=False),
    Column("platform", Text, nullable=False),
    Column("state", Text, nullable=False, server_default=text("'uploading'")),
    Column("idempotency_key", Text, nullable=False),  # "<job id>:<account id>"
    Column("session_url", Text),  # the platform's upload session, so a crashed upload is resumed, never repeated
    Column("remote_id", Text),
    Column("remote_url", Text),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("error", Text),
    _stamp("created_at"),
    _stamp("updated_at"),
    Column("posted_at", DateTime(timezone=True)),
    CheckConstraint("state IN ('uploading', 'posted', 'failed', 'skipped')", name="ck_posts_state"),
    UniqueConstraint("tenant_id", "job_id", "account_id", name="uq_posts_job_account"),
    Index("ix_posts_account_created", "account_id", "created_at"),
)

matches = Table(
    "matches",
    metadata,
    _id(),
    _tenant(),
    Column("stream_id", UUID(as_uuid=True), ForeignKey("live_streams.id", ondelete="CASCADE"), nullable=False),
    Column("start_s", Numeric(12, 3), nullable=False),  # on the stream clock
    Column("end_s", Numeric(12, 3), nullable=False),
    Column("summary", Text),
    Column("status", Text, nullable=False, server_default=text("'detected'")),
    Column("job_id", UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL")),  # the recap made from it
    _stamp("created_at"),
    CheckConstraint("start_s >= 0 AND end_s > start_s", name="ck_matches_range"),
    CheckConstraint("status IN ('detected', 'compiled', 'skipped')", name="ck_matches_status"),
    UniqueConstraint("tenant_id", "stream_id", "start_s", name="uq_matches_stream_start"),
)

hosted_files = Table(  # clips kept in R2 for a while so they can be downloaded: see services/publisher/hosted.py
    "hosted_files",
    metadata,
    _id(),
    _tenant(),
    Column("post_id", UUID(as_uuid=True), ForeignKey("posts.id", ondelete="CASCADE")),
    Column("key", Text, nullable=False),
    Column("bytes", BigInteger, nullable=False),
    Column("chats", JSONB, nullable=False, server_default=text("'[]'::jsonb")),  # where you were told about it
    _stamp("created_at"),
    Column("delete_requested_at", DateTime(timezone=True)),
    Column("deleted_at", DateTime(timezone=True)),
    Column(
        "keep_until", DateTime(timezone=True)
    ),  # due for deletion from this time; none: by age (media_host.max_age_h)
    CheckConstraint("bytes >= 0", name="ck_hosted_files_bytes"),
    UniqueConstraint("tenant_id", "key", name="uq_hosted_files_key"),
    Index("ix_hosted_files_live", "deleted_at", "created_at"),
)

heartbeats = Table(  # every service says it is alive every half minute: see common/heartbeat.py
    "heartbeats",
    metadata,
    _tenant(),
    Column("service", Text, nullable=False),
    Column("instance", Text, nullable=False),
    _stamp("started_at"),
    _stamp("beat_at"),
    PrimaryKeyConstraint("tenant_id", "service", "instance", name="pk_heartbeats"),
)

settings = Table(  # values changed while the system runs (the budget cap), with who changed them: see common/spend.py
    "settings",
    metadata,
    _tenant(),
    Column("key", Text, nullable=False),
    Column("value", JSONB, nullable=False),
    Column("updated_by", Text, nullable=False, server_default=text("''")),
    _stamp("updated_at"),
    PrimaryKeyConstraint("tenant_id", "key", name="pk_settings"),
)

pauses = Table(  # something shared is used up until a time (an LLM quota): see common/pauses.py
    "pauses",
    metadata,
    _tenant(),
    Column("key", Text, nullable=False),
    Column("until", DateTime(timezone=True), nullable=False),
    Column("reason", Text, nullable=False, server_default=text("''")),
    _stamp("created_at"),
    PrimaryKeyConstraint("tenant_id", "key", name="pk_pauses"),
)

costs = Table(
    "costs",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    _tenant(),
    Column("job_id", UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL")),
    Column("profile", Text, nullable=False),
    Column("service", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("units", Numeric(18, 4), nullable=False),
    Column("unit", Text, nullable=False),
    Column("usd", Numeric(12, 6), nullable=False, server_default=text("0")),
    _stamp("created_at"),
    CheckConstraint("usd >= 0 AND units >= 0", name="ck_costs_not_negative"),
    Index("ix_costs_profile_created_at", "profile", "created_at"),
    Index("ix_costs_service_unit_created_at", "service", "unit", "created_at"),
)
