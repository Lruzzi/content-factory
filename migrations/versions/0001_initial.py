"""initial schema: tenants, sources, jobs, live_streams, costs

Revision ID: 0001
Revises:
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

DEFAULT_TENANT = "'00000000-0000-0000-0000-000000000001'::uuid"


def _id() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()"))


def _tenant() -> sa.Column:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id"),
        nullable=False,
        server_default=sa.text(DEFAULT_TENANT),
    )


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))


def upgrade() -> None:
    op.create_table("tenants", _id(), sa.Column("name", sa.Text(), nullable=False, unique=True), _stamp("created_at"))
    op.execute("INSERT INTO tenants (id, name) VALUES ('00000000-0000-0000-0000-000000000001', 'default')")

    op.create_table(
        "sources",
        _id(),
        _tenant(),
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("name", sa.Text()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("clip_policy", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("overrides", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("paused_at", sa.DateTime(timezone=True)),
        sa.Column("pause_reason", sa.Text()),
        sa.Column("claim_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_polled_at", sa.DateTime(timezone=True)),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.Column("updated_by", sa.Text()),
        sa.CheckConstraint("clip_policy IN ('open', 'blocked')", name="ck_sources_clip_policy"),
        sa.CheckConstraint("pause_reason IN ('manual', 'claim')", name="ck_sources_pause_reason"),
        sa.CheckConstraint("(paused_at IS NULL) = (pause_reason IS NULL)", name="ck_sources_pause_pair"),
        sa.UniqueConstraint("tenant_id", "profile", "source_key", name="uq_sources_profile_key"),
    )

    op.create_table(
        "jobs",
        _id(),
        _tenant(),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id")),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("kind", sa.Text()),
        sa.Column("spec", postgresql.JSONB(), nullable=False),
        sa.Column("media_key", sa.Text()),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("retry_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.CheckConstraint("status IN ('queued', 'running', 'done', 'failed', 'dead')", name="ck_jobs_status"),
        sa.CheckConstraint("attempt >= 0", name="ck_jobs_attempt"),
        sa.CheckConstraint(
            "kind IS NULL OR kind IN ('highlight', 'donation', 'tutorial', 'news', 'funny')", name="ck_jobs_kind"
        ),
    )
    op.create_index("ix_jobs_status_retry_at", "jobs", ["status", "retry_at"])

    op.create_table(
        "live_streams",
        _id(),
        _tenant(),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id")),
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'live'")),
        _stamp("started_at"),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("lease_worker", sa.Text()),
        sa.Column("lease_heartbeat_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('live', 'ended')", name="ck_live_streams_status"),
        sa.CheckConstraint("(lease_worker IS NULL) = (lease_heartbeat_at IS NULL)", name="ck_live_streams_lease_pair"),
        sa.UniqueConstraint("tenant_id", "video_id", name="uq_live_streams_video"),
    )

    op.create_table(
        "costs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        _tenant(),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="SET NULL")),
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("units", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("usd", sa.Numeric(12, 6), nullable=False, server_default=sa.text("0")),
        _stamp("created_at"),
        sa.CheckConstraint("usd >= 0 AND units >= 0", name="ck_costs_not_negative"),
    )
    op.create_index("ix_costs_profile_created_at", "costs", ["profile", "created_at"])
    op.create_index("ix_costs_service_unit_created_at", "costs", ["service", "unit", "created_at"])


def downgrade() -> None:
    for table in ("costs", "live_streams", "jobs", "sources", "tenants"):
        op.drop_table(table)
