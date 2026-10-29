"""hosted_files.keep_until: when a stored clip is due to be deleted (a rejected clip at once, an undecided one when its
review expires, an approved one 24 hours after approval)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosted_files", sa.Column("keep_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("hosted_files", "keep_until")
