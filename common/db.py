"""Database access: the engine and the migrations."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine

ROOT = Path(__file__).resolve().parent.parent


def database_url(environ: Mapping[str, str] = os.environ) -> str:
    """The URL named by `database.url_env` in workers.yaml (DATABASE_URL)."""
    url = environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def sqlstate(exc: BaseException) -> str | None:
    """The 5-character SQLSTATE of a database error ("23505" unique, "23514" check, "23503" foreign key).

    Read this instead of the exception class: pg8000 raises IntegrityError only for unique violations and
    ProgrammingError for check and foreign-key ones, while psycopg uses IntegrityError for all of them.
    """
    orig = getattr(exc, "orig", exc)
    code = getattr(orig, "sqlstate", None)  # psycopg
    if code:
        return str(code)
    args = getattr(orig, "args", ())
    if args and isinstance(args[0], dict):  # pg8000
        found = args[0].get("C")
        return str(found) if found else None
    return None


def make_engine(url: str | None = None) -> Engine:
    return create_engine(url or database_url(), pool_pre_ping=True)


def migrate(engine: Engine, revision: str = "head") -> None:
    """Bring the database to `revision` (default: the newest schema)."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, revision)
