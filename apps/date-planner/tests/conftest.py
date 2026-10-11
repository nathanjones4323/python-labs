"""Shared fixtures: a migrated test database, wiped between tests.

Database tests need `DATE_PLANNER_TEST_DATABASE_URL` (see docker-compose.yml)
and are skipped without it, so the pure tests still run anywhere. The schema
is applied with Alembic rather than `create_all`, so the migrations are
exercised too.
"""

import argparse
import os
from collections.abc import AsyncIterator
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from date_planner.booking.channels import BookingChannel, WindowUnit
from date_planner.config import get_settings
from date_planner.db import Base, make_session_factory, repo, session_factory
from date_planner.db.models import DateStatus
from date_planner.schemas import CoupleProfileIn, DatePatch, RestaurantIn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TEST_DATABASE_URL = os.environ.get("DATE_PLANNER_TEST_DATABASE_URL", "").strip()
APP_DIR = Path(__file__).resolve().parent.parent
LA = ZoneInfo("America/Los_Angeles")


@pytest.fixture(scope="session")
def migrated() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("DATE_PLANNER_TEST_DATABASE_URL not set")
    config = Config(APP_DIR / "alembic.ini")
    config.set_main_option("script_location", str(APP_DIR / "migrations"))
    # env.py reads the URL from -x url=..., which is how the Alembic CLI's
    # own `-x` arrives too.
    config.cmd_opts = argparse.Namespace(x=[f"url={TEST_DATABASE_URL}"])
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    return TEST_DATABASE_URL


async def _dispose(factory: async_sessionmaker[AsyncSession]) -> None:
    engine = factory.kw["bind"]
    await engine.dispose()


@pytest.fixture
async def db(
    migrated: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory over an empty schema; the tools see the same database.

    The engine is per-test because each test gets its own event loop, and a
    connection pool belongs to the loop that opened it.
    """
    monkeypatch.setenv("DATABASE_URL", migrated)
    get_settings.cache_clear()
    session_factory.cache_clear()
    factory = make_session_factory(migrated)
    async with factory() as session, session.begin():
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await session.execute(text(f"TRUNCATE {tables} CASCADE"))
    yield factory
    await _dispose(factory)
    # The tools' own factory, if a test went through them.
    await _dispose(session_factory())
    session_factory.cache_clear()
    get_settings.cache_clear()


@pytest.fixture
async def seeded(db: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """One couple, a phone-only venue (Nobu) and a Resy venue, plus two past
    dates — one loved, one middling."""
    async with db() as session, session.begin():
        couple = await repo.upsert_couple(
            session,
            CoupleProfileIn(
                name="Test couple",
                member_emails=["a@example.com", "b@example.com"],
                shared_calendar_id="shared@group.calendar.google.com",
                home_area="Santa Monica",
                timezone="America/Los_Angeles",
                budget_per_date=250,
                dietary=["no shellfish"],
                likes=["sushi"],
                dislikes=["loud rooms"],
                max_travel_minutes=45,
            ),
        )
        nobu = await repo.upsert_restaurant(
            session,
            RestaurantIn(
                name="Nobu Malibu",
                city="Malibu",
                phone="+13103179140",
                website="https://nobu.example.com",
                timezone="America/Los_Angeles",
                booking_channel=BookingChannel.PHONE,
                window_amount=1,
                window_unit=WindowUnit.MONTHS,
                window_open_time=time(10, 0),
            ),
        )
        bestia = await repo.upsert_restaurant(
            session,
            RestaurantIn(
                name="Bestia",
                city="Los Angeles",
                website="https://bestia.example.com",
                timezone="America/Los_Angeles",
                booking_channel=BookingChannel.RESY,
                platform_slug="bestia",
                platform_city_slug="los-angeles-ca",
            ),
        )
        now = datetime.now(timezone.utc)
        loved = await repo.create_date(
            session,
            couple_id=couple.id,
            title="Sushi at Nobu",
            start_at=now - timedelta(days=30),
            end_at=now - timedelta(days=30) + timedelta(hours=2),
            restaurant_id=nobu.id,
        )
        meh = await repo.create_date(
            session,
            couple_id=couple.id,
            title="Pasta at Bestia",
            start_at=now - timedelta(days=10),
            end_at=now - timedelta(days=10) + timedelta(hours=2),
            restaurant_id=bestia.id,
        )
        # Both happened; one was a hit. Walk the real state machine to get
        # there, rather than writing `completed` straight in.
        for planned, rating in ((loved, 5), (meh, 3)):
            for patch in (
                DatePatch(status=DateStatus.TENTATIVE),
                DatePatch(status=DateStatus.CONFIRMED),
                DatePatch(status=DateStatus.COMPLETED, rating=rating),
            ):
                await repo.update_date(session, planned.id, patch)
        return {
            "couple_id": couple.id,
            "nobu_id": nobu.id,
            "bestia_id": bestia.id,
            "loved_date_id": loved.id,
            "meh_date_id": meh.id,
        }
