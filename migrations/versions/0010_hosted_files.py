"""hosted_files: clips kept in R2 for a while so they can be downloaded, with who asked to delete them

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

DEFAULT_TENANT = "'00000000-0000-0000-0000-000000000001'::uuid"


def upgrade() -> None:
    op.create_table(
        "hosted_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            server_default=sa.text(DEFAULT_TENANT),
        ),
        sa.Column("post_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("posts.id", ondelete="CASCADE")),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("chats", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("delete_requested_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("bytes >= 0", name="ck_hosted_files_bytes"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_hosted_files_key"),
    )
    op.create_index("ix_hosted_files_live", "hosted_files", ["deleted_at", "created_at"])


def downgrade() -> None:
    op.drop_table("hosted_files")
