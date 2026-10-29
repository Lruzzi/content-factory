"""matches (the finished matches found in a stream) and the `recap` kind of job

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

DEFAULT_TENANT = "'00000000-0000-0000-0000-000000000001'::uuid"
KINDS = "'highlight', 'donation', 'tutorial', 'news', 'funny'"


def upgrade() -> None:
    op.drop_constraint("ck_jobs_kind", "jobs")
    op.create_check_constraint("ck_jobs_kind", "jobs", f"kind IS NULL OR kind IN ({KINDS}, 'recap')")
    op.create_table(
        "matches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            server_default=sa.text(DEFAULT_TENANT),
        ),
        sa.Column(
            "stream_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("live_streams.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("start_s", sa.Numeric(12, 3), nullable=False),
        sa.Column("end_s", sa.Numeric(12, 3), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'detected'")),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("start_s >= 0 AND end_s > start_s", name="ck_matches_range"),
        sa.CheckConstraint("status IN ('detected', 'compiled', 'skipped')", name="ck_matches_status"),
        sa.UniqueConstraint("tenant_id", "stream_id", "start_s", name="uq_matches_stream_start"),
    )


def downgrade() -> None:
    op.drop_table("matches")
    op.drop_constraint("ck_jobs_kind", "jobs")
    op.create_check_constraint("ck_jobs_kind", "jobs", f"kind IS NULL OR kind IN ({KINDS})")
