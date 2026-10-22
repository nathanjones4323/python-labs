"""Alembic environment: migrate the schema in `pay_my_bills.db.models`.

The URL comes from `-x url=...` or PAY_MY_BILLS_DATABASE_URL (default: the
SQLite file under ~/.local/share/pay-my-bills), never from alembic.ini.
"""

from logging.config import fileConfig

from alembic import context
from pay_my_bills.config import database_url
from pay_my_bills.db import make_engine
from pay_my_bills.db.models import Base, UTCDateTime

config = context.config
if config.config_file_name is not None:
    # Keep the app's own loggers (the bot migrates at start-up).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _render_item(type_, obj, autogen_context):
    """Write `UTCDateTime` columns as plain `sa.DateTime()`, so a migration
    doesn't import the app (and break when the app's code moves)."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime()"
    return False


def _database_url() -> str:
    return context.get_x_argument(as_dictionary=True).get("url") or database_url()


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
            render_item=_render_item,
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
