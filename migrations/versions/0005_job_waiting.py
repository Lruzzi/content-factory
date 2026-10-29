"""jobs.status 'waiting': a job held for a person (the Telegram review) until they decide

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-23
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_jobs_status", "jobs")
    op.create_check_constraint(
        "ck_jobs_status", "jobs", "status IN ('queued', 'running', 'waiting', 'done', 'failed', 'dead')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_jobs_status", "jobs")
    op.create_check_constraint("ck_jobs_status", "jobs", "status IN ('queued', 'running', 'done', 'failed', 'dead')")
