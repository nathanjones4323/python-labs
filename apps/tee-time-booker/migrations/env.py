"""Alembic environment: migrate the schema in `tee_time_booker.db.models`.

The URL comes from DATABASE_URL (or `-x url=...`), never from alembic.ini, so
the same config runs locally, in CI and against the hosted database.
"""

import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from tee_time_booker.db import make_engine, normalize_url
from tee_time_booker.db.models import Base

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


def run_migrations_online() -> None:
    engine = make_engine(_database_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite can't ALTER most things; batch mode rebuilds the table.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
