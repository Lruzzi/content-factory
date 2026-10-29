"""posts: one row per clip per account, written before the upload starts

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

DEFAULT_TENANT = "'00000000-0000-0000-0000-000000000001'::uuid"


def upgrade() -> None:
    op.create_table(
        "posts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            server_default=sa.text(DEFAULT_TENANT),
        ),
        sa.Column(
            "job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'uploading'")),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("session_url", sa.Text()),
        sa.Column("remote_id", sa.Text()),
        sa.Column("remote_url", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("state IN ('uploading', 'posted', 'failed', 'skipped')", name="ck_posts_state"),
        sa.UniqueConstraint("tenant_id", "job_id", "account_id", name="uq_posts_job_account"),
    )
    op.create_index("ix_posts_account_created", "posts", ["account_id", "created_at"])


def downgrade() -> None:
    op.drop_table("posts")
