import os
from contextlib import contextmanager
from collections.abc import Generator

from psycopg import Connection
from psycopg_pool import ConnectionPool

_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/app",
)

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(_DATABASE_URL, open=True)
    return _pool


@contextmanager
def get_conn() -> Generator[Connection, None, None]:
    with get_pool().connection() as conn:
        yield conn
