"""Test helpers for database errors."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import sqlalchemy as sa

from common.db import sqlstate

UNIQUE, FOREIGN_KEY, CHECK = "23505", "23503", "23514"


@contextmanager
def violation(code: str) -> Iterator[None]:
    """Expect the body to fail with this SQLSTATE, whatever exception class the driver uses."""
    with pytest.raises(sa.exc.DBAPIError) as caught:
        yield
    assert sqlstate(caught.value) == code
