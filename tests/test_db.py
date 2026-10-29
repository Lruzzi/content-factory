"""Schema, migrations and constraints (NFR-12: tenant_id everywhere, time-ordered ids)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from db_helpers import CHECK, UNIQUE, violation

from common.models import DEFAULT_TENANT_ID, metadata

pytestmark = pytest.mark.integration

TABLES = {"tenants", "sources", "jobs", "live_streams", "costs"}


def insert_source(conn, **overrides):
    values = {"profile": "mlbb", "source_key": "someone", "url": "https://www.youtube.com/@someone"} | overrides
    return conn.execute(
        sa.text(
            "INSERT INTO sources (profile, source_key, url, clip_policy, paused_at, pause_reason) "
            "VALUES (:profile, :source_key, :url, COALESCE(:clip_policy, 'open'), :paused_at, :pause_reason) "
            "RETURNING id, tenant_id"
        ),
        {"clip_policy": None, "paused_at": None, "pause_reason": None} | values,
    ).one()


def insert_job(conn, *, status=None, spec="{}"):
    conn.execute(
        sa.text(
            "INSERT INTO jobs (stage, status, spec) "
            "VALUES ('select', COALESCE(:status, 'queued'), CAST(:spec AS jsonb))"
        ),
        {"status": status, "spec": spec},
    )


def test_the_migration_creates_every_table(db):
    assert TABLES <= set(sa.inspect(db).get_table_names())


def test_the_models_match_the_migrated_database(db):
    with db.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), metadata) == []


def test_the_default_tenant_is_seeded(db):
    with db.connect() as conn:
        assert conn.execute(sa.text("SELECT id FROM tenants")).scalars().all() == [DEFAULT_TENANT_ID]


def test_every_data_table_has_a_tenant_id():
    for name in TABLES - {"tenants"}:
        assert "tenant_id" in metadata.tables[name].c, name


def test_a_new_row_gets_the_default_tenant_and_a_time_ordered_id(db):
    with db.begin() as conn:
        row = insert_source(conn)
    assert row.tenant_id == DEFAULT_TENANT_ID
    assert uuid.UUID(str(row.id)).version == 7


def test_clip_policy_only_accepts_open_or_blocked(db):
    with violation(CHECK), db.begin() as conn:
        insert_source(conn, clip_policy="maybe")


def test_a_source_key_is_unique_within_a_profile(db):
    with db.begin() as conn:
        insert_source(conn)
    with violation(UNIQUE), db.begin() as conn:
        insert_source(conn)


def test_the_same_source_key_may_exist_in_another_profile(db):
    with db.begin() as conn:
        insert_source(conn)
        insert_source(conn, profile="podcast_id")


def test_a_pause_needs_a_reason(db):
    with violation(CHECK), db.begin() as conn:
        insert_source(conn, paused_at=datetime.now(UTC))


def test_a_pause_reason_needs_a_pause(db):
    with violation(CHECK), db.begin() as conn:
        insert_source(conn, pause_reason="manual")


def test_a_job_status_must_be_known(db):
    with violation(CHECK), db.begin() as conn:
        insert_job(conn, status="weird")


def test_a_job_keeps_its_spec_snapshot(db):
    with db.begin() as conn:
        insert_job(conn, spec='{"selection": {"top_k": 9}}')
    with db.connect() as conn:
        assert conn.execute(sa.text("SELECT spec->'selection'->>'top_k' FROM jobs")).scalar_one() == "9"


def test_a_new_job_starts_queued_with_no_attempts(db):
    with db.begin() as conn:
        insert_job(conn)
    with db.connect() as conn:
        status, attempt = conn.execute(sa.text("SELECT status, attempt FROM jobs")).one()
    assert (status, attempt) == ("queued", 0)
