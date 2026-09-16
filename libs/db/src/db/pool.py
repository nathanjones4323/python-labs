import os
from contextlib import contextmanager
from collections.abc import Generator

from psycopg import Connection
from psycopg_pool import ConnectionPool

_RAW_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/app",
)
# Alembic/SQLAlchemy want a driver-qualified URL (postgresql+psycopg://), but
# libpq — and therefore psycopg_pool — only understands postgresql://.
_DATABASE_URL = _RAW_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)

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
