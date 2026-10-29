"""heartbeats: every service says it is alive every half minute

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

DEFAULT_TENANT = "'00000000-0000-0000-0000-000000000001'::uuid"


def upgrade() -> None:
    op.create_table(
        "heartbeats",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            server_default=sa.text(DEFAULT_TENANT),
        ),
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("instance", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("tenant_id", "service", "instance", name="pk_heartbeats"),
    )


def downgrade() -> None:
    op.drop_table("heartbeats")
