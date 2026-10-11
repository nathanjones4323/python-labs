"""Connecting to Postgres.

Tools are async (one of them fetches four web pages), so the engine is async
too. `open_session()` is what a tool uses: it opens a session on the process
engine, runs the body in one transaction and commits on the way out.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from date_planner.config import get_settings


def normalize_url(url: str) -> str:
    """SQLAlchemy wants the driver in the scheme; hosted DBs hand out plain
    `postgres://`."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    return url


def make_engine(url: str) -> AsyncEngine:
    # pool_pre_ping: an MCP server sits idle between questions, long enough
    # for a hosted Postgres to have dropped the connection.
    return create_async_engine(normalize_url(url), pool_pre_ping=True)


def make_session_factory(url: str) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(make_engine(url), expire_on_commit=False)


@lru_cache(maxsize=1)
def session_factory() -> async_sessionmaker[AsyncSession]:
    """The process-wide factory, built from DATABASE_URL on first use."""
    return make_session_factory(get_settings().require_database_url())


@asynccontextmanager
async def open_session() -> AsyncIterator[AsyncSession]:
    """A session wrapping one unit of work, committed if the body returns."""
    async with session_factory()() as session, session.begin():
        yield session
