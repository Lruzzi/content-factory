"""live_streams.recap_checked_s: how far into the stream the match finder has last looked

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("live_streams", sa.Column("recap_checked_s", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("live_streams", "recap_checked_s")
