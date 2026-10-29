import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import redis
    import sqlalchemy

# tests run against the repo's own config, whatever the developer's shell exports
os.environ.pop("CONFIG_DIR", None)

# config/ is not a package: `config_schema` is imported as a top-level module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "config"))

NEEDS_SERVICES = "{} is not set: run `make itest` (it starts Postgres and Redis) or export it"


@pytest.fixture
def stream_prefix() -> str:
    """A unique name prefix, so tests never touch each other's streams."""
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def redis_client(stream_prefix: str) -> Iterator[redis.Redis]:
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip(NEEDS_SERVICES.format("TEST_REDIS_URL"))
    import redis

    client = redis.Redis.from_url(url, decode_responses=True)
    yield client
    for key in client.scan_iter(match=f"{stream_prefix}*"):
        client.delete(key)
    client.close()


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[sqlalchemy.Engine]:
    """A fresh database, migrated to head, for the whole test session."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip(NEEDS_SERVICES.format("TEST_DATABASE_URL"))
    import sqlalchemy as sa

    from common.db import migrate

    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    name = f"cf_test_{uuid.uuid4().hex[:10]}"  # generated here, never user input
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))  # noqa: S608
    engine = sa.create_engine(sa.make_url(url).set(database=name))
    migrate(engine)
    yield engine
    engine.dispose()
    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE "{name}" WITH (FORCE)'))  # noqa: S608
    admin.dispose()


@pytest.fixture
def db(pg_engine: sqlalchemy.Engine) -> Iterator[sqlalchemy.Engine]:
    """The migrated engine; every table but the seeded tenant is emptied after each test."""
    import sqlalchemy as sa

    yield pg_engine
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "TRUNCATE sources, jobs, live_streams, costs, pauses, hosted_files, heartbeats, settings "
                "RESTART IDENTITY CASCADE"
            )
        )
