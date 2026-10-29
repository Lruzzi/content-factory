"""segments.deleted_at: when the recording file was removed to free disk (the row stays as the record)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("segments", sa.Column("deleted_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("segments", "deleted_at")
