"""chat_counts: messages per 10-second bucket of a live stream (no names, no text), and the chat listener's own lease

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

DEFAULT_TENANT = "'00000000-0000-0000-0000-000000000001'::uuid"


def upgrade() -> None:
    op.create_table(
        "chat_counts",
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
        sa.Column("bucket_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "stream_id", "bucket_at", name="pk_chat_counts"),
        sa.CheckConstraint("n >= 0", name="ck_chat_counts_n"),
    )
    op.add_column("live_streams", sa.Column("chat_worker", sa.Text(), nullable=True))
    op.add_column("live_streams", sa.Column("chat_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_live_streams_chat_lease_pair", "live_streams", "(chat_worker IS NULL) = (chat_heartbeat_at IS NULL)"
    )


def downgrade() -> None:
    op.drop_constraint("ck_live_streams_chat_lease_pair", "live_streams", type_="check")
    op.drop_column("live_streams", "chat_heartbeat_at")
    op.drop_column("live_streams", "chat_worker")
    op.drop_table("chat_counts")
