"""transcripts: what was said in each recorded segment, on the stream clock

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

DEFAULT_TENANT = "'00000000-0000-0000-0000-000000000001'::uuid"


def upgrade() -> None:
    op.create_table(
        "transcripts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            server_default=sa.text(DEFAULT_TENANT),
        ),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="SET NULL")),
        sa.Column("stream_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("live_streams.id", ondelete="CASCADE")),
        sa.Column("media_key", sa.Text(), nullable=False),
        sa.Column("start_s", sa.Numeric(12, 3), nullable=False),
        sa.Column("duration_s", sa.Numeric(12, 3), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("sentences", postgresql.JSONB(), nullable=False),
        sa.Column("words", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("start_s >= 0 AND duration_s >= 0", name="ck_transcripts_range"),
        sa.UniqueConstraint("tenant_id", "media_key", name="uq_transcripts_media"),
    )
    op.create_index("ix_transcripts_stream_start", "transcripts", ["stream_id", "start_s"])


def downgrade() -> None:
    op.drop_table("transcripts")
