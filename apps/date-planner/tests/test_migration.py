"""The migrations: they build the schema, tear it down, and match the models.

Synchronous on purpose: Alembic's env.py runs its own event loop, which it
can't do inside an async test's.
"""

import asyncio
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from conftest import alembic_config
from date_planner.db import make_engine
from date_planner.db.models import Base
from sqlalchemy import text

TABLES = {
    "alembic_version",
    "couple_members",
    "couples",
    "date_stops",
    "dates",
    "travel_cache",
    "venues",
}


def _query(url: str, query: str) -> list[dict[str, Any]]:
    async def go() -> list[dict[str, Any]]:
        engine = make_engine(url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text(query))
                return [dict(row._mapping) for row in result]
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _drift(url: str) -> list[Any]:
    """What autogenerate would still change to make the database match the models."""

    async def go() -> list[Any]:
        engine = make_engine(url)
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(
                    lambda sync: compare_metadata(
                        MigrationContext.configure(sync), Base.metadata
                    )
                )
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_upgrade_builds_the_schema_and_downgrade_removes_it(migrated: str) -> None:
    config = alembic_config(migrated)
    tables = "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    enums = "SELECT typname FROM pg_type WHERE typtype = 'e'"
    try:
        command.downgrade(config, "base")
        # Only Alembic's own bookkeeping is left, and no enum types.
        assert {r["tablename"] for r in _query(migrated, tables)} <= {"alembic_version"}
        assert _query(migrated, enums) == []

        command.upgrade(config, "head")
        assert {r["tablename"] for r in _query(migrated, tables)} == TABLES
    finally:
        # Leave the schema where the rest of the suite expects it.
        command.upgrade(config, "head")


def test_the_migration_matches_the_models(migrated: str) -> None:
    config = alembic_config(migrated)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    assert _drift(migrated) == []
