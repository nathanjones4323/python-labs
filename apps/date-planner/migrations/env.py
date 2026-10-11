"""Alembic environment: migrate the schema in `date_planner.db.models`.

The URL comes from DATABASE_URL (or `-x url=...`), never from alembic.ini, so
the same config runs locally, in CI and against a hosted database. The engine
is async (asyncpg), so the online path drives the migrations through
`run_sync`.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from date_planner.db import make_engine, normalize_url
from date_planner.db.models import Base
from dotenv import load_dotenv
from sqlalchemy import Connection

load_dotenv()

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("url") or os.environ.get(
        "DATABASE_URL", ""
    )
    if not url:
        raise RuntimeError("Set DATABASE_URL (or pass -x url=...) to run migrations")
    return normalize_url(url)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = make_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
