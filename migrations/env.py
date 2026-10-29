"""Alembic environment. Online only: it runs against DATABASE_URL, or a connection passed in by `common.db.migrate`."""

import os

from alembic import context
from sqlalchemy import create_engine, pool

from common.models import metadata

config = context.config


def run_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _migrate(connection)
        return
    engine = create_engine(os.environ["DATABASE_URL"], poolclass=pool.NullPool)
    with engine.connect() as conn:
        _migrate(conn)


def _migrate(connection) -> None:
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported: run against a database")
run_online()
