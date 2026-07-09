"""Shared test fixtures.

Database strategy (spec 11B, behavior 8): tests open a real psycopg connection
to the Docker Postgres (localhost:5433), start a REPEATABLE READ transaction,
INSERT synthetic rows, exercise the code under test inside that same
uncommitted transaction, and roll back on teardown so nothing persists.

REPEATABLE READ matters: the live producer/consumer stack may be streaming
rows into `transactions` concurrently. A snapshot taken at the first statement
of our transaction freezes the visible "live" rows, so baseline counts taken
inside the transaction stay consistent with later queries in the same
transaction (plus our own uncommitted inserts).
"""

import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

# The project is not installed as a package (no build-system in pyproject), so
# pytest does not put the repo root on sys.path by itself. Prepend it here so
# test modules can import the application packages (mcp_server, consumer, ...).
# conftest.py is loaded by pytest before any test module import.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_FALLBACK_DSN = "host=localhost port=5433 dbname=streaming_rag user=rag password=localdev"


def _dsn() -> str:
    """Prefer the project's own DSN config; fall back to the documented dev DSN."""
    try:
        from consumer.config import POSTGRES_DSN, POSTGRES_PASSWORD

        if POSTGRES_PASSWORD:
            return POSTGRES_DSN
    except Exception:
        pass
    return _FALLBACK_DSN


def connect_db(**kwargs) -> psycopg.Connection:
    """Open a real connection to the dev Postgres with dict rows."""
    return psycopg.connect(_dsn(), row_factory=dict_row, **kwargs)


@pytest.fixture()
def connect_db_factory():
    """The raw connection factory, for tests that manage their own txn."""
    return connect_db


@pytest.fixture()
def db_conn():
    """A non-autocommit connection in REPEATABLE READ; always rolled back."""
    conn = connect_db()
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
