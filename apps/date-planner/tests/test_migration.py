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
    "date_ratings",
    "date_stops",
    "dates",
    "stop_ratings",
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


def _execute(url: str, *statements: str) -> None:
    async def go() -> None:
        engine = make_engine(url)
        try:
            async with engine.begin() as conn:
                for statement in statements:
                    await conn.execute(text(statement))
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_a_one_to_five_rating_becomes_each_members_zero_to_ten(migrated: str) -> None:
    config = alembic_config(migrated)
    couple, alex, sam, rated, unrated = (
        f"00000000-0000-0000-0000-00000000000{n}" for n in range(1, 6)
    )
    try:
        command.downgrade(config, "002")
        _execute(
            migrated,
            f"INSERT INTO couples (id, name, timezone) VALUES ('{couple}', 'C', 'UTC')",
            "INSERT INTO couple_members (id, couple_id, name, likes, dislikes, dietary)"
            f" VALUES ('{alex}', '{couple}', 'Alex', '{{}}', '{{}}', '{{}}'),"
            f" ('{sam}', '{couple}', 'Sam', '{{}}', '{{}}', '{{}}')",
            "INSERT INTO dates (id, couple_id, title, day, max_leg_minutes, status,"
            f" rating) VALUES ('{rated}', '{couple}', 'R', '2026-01-01', 25,"
            f" 'completed', 4), ('{unrated}', '{couple}', 'U', '2026-01-02', 25,"
            " 'completed', NULL)",
        )
        command.upgrade(config, "003")
        rows = _query(
            migrated,
            "SELECT date_id::text, member_id::text, score, notes, source::text"
            " FROM date_ratings ORDER BY member_id",
        )
        assert [(r["date_id"], r["member_id"], r["score"]) for r in rows] == [
            (rated, alex, 8),
            (rated, sam, 8),
        ]
        assert {r["source"] for r in rows} == {"chat"}
        assert all(r["notes"].startswith("Migrated") for r in rows)

        # Back down: the average, halved.
        _execute(
            migrated, f"UPDATE date_ratings SET score = 10 WHERE member_id = '{sam}'"
        )
        command.downgrade(config, "002")
        back = _query(migrated, "SELECT id::text, rating FROM dates ORDER BY id")
        assert [(r["id"], r["rating"]) for r in back] == [
            (rated, 5),
            (unrated, None),
        ]
    finally:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
