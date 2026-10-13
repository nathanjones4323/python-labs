"""Shared fixtures: a migrated test database, wiped between tests.

Database tests need `DATE_PLANNER_TEST_DATABASE_URL` (see docker-compose.yml)
and are skipped without it, so the pure tests still run anywhere. The schema
is applied with Alembic rather than `create_all`, so the migrations are
exercised too.
"""

import argparse
import os
from collections.abc import AsyncIterator
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from date_planner.booking.channels import BookingChannel, WindowUnit
from date_planner.config import get_settings
from date_planner.db import Base, make_session_factory, repo, session_factory
from date_planner.db.models import (
    Anchor,
    ReservationNeed,
    StopBookingStatus,
    StopKind,
    VenueType,
)
from date_planner.schemas import (
    CoupleProfileIn,
    MemberIn,
    PreferenceEdit,
    StopIn,
    VenueIn,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TEST_DATABASE_URL = os.environ.get("DATE_PLANNER_TEST_DATABASE_URL", "").strip()
APP_DIR = Path(__file__).resolve().parent.parent
LA = ZoneInfo("America/Los_Angeles")


def alembic_config(url: str) -> Config:
    config = Config(APP_DIR / "alembic.ini")
    config.set_main_option("script_location", str(APP_DIR / "migrations"))
    # env.py reads the URL from -x url=..., which is how the Alembic CLI's
    # own `-x` arrives too.
    config.cmd_opts = argparse.Namespace(x=[f"url={url}"])
    return config


@pytest.fixture(scope="session")
def migrated() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("DATE_PLANNER_TEST_DATABASE_URL not set")
    config = alembic_config(TEST_DATABASE_URL)
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
    """One couple; a phone-only venue (Nobu) and a Resy one (Bestia); the
    Malibu day's trail and museum; and two past one-stop dates — one loved,
    one middling."""
    async with db() as session, session.begin():
        couple = await repo.upsert_couple(
            session,
            CoupleProfileIn(
                name="Test couple",
                shared_calendar_id="shared@group.calendar.google.com",
                home_area="Santa Monica",
                timezone="America/Los_Angeles",
                budget_per_date=250,
                max_travel_minutes=45,
            ),
        )
        alex = await repo.upsert_member(
            session, couple.id, MemberIn(name="Alex", email="a@example.com")
        )
        sam = await repo.upsert_member(
            session, couple.id, MemberIn(name="Sam", email="b@example.com")
        )
        await repo.edit_preferences(
            session,
            couple.id,
            "Alex",
            PreferenceEdit(add_likes=["sushi"], add_dietary=["no shellfish"]),
        )
        await repo.edit_preferences(
            session,
            couple.id,
            "Sam",
            PreferenceEdit(add_likes=["hiking"], add_dislikes=["loud rooms"]),
        )
        nobu = await repo.upsert_venue(
            session,
            VenueIn(
                name="Nobu Malibu",
                city="Malibu",
                venue_type=VenueType.RESTAURANT,
                phone="+13103179140",
                website="https://nobu.example.com",
                timezone="America/Los_Angeles",
                lat=34.0389596,
                lng=-118.6696041,
                default_duration_min=90,
                booking_channel=BookingChannel.PHONE,
                window_amount=1,
                window_unit=WindowUnit.MONTHS,
                window_open_time=time(10, 0),
            ),
        )
        bestia = await repo.upsert_venue(
            session,
            VenueIn(
                name="Bestia",
                city="Los Angeles",
                venue_type=VenueType.RESTAURANT,
                website="https://bestia.example.com",
                timezone="America/Los_Angeles",
                booking_channel=BookingChannel.RESY,
                platform_slug="bestia",
                platform_city_slug="los-angeles-ca",
            ),
        )
        solstice = await repo.upsert_venue(
            session,
            VenueIn(
                name="Solstice Canyon",
                city="Malibu",
                venue_type=VenueType.TRAIL,
                lat=34.03765,
                lng=-118.7477,
                external_url=(
                    "https://www.alltrails.com/trail/us/california/"
                    "solstice-canyon-trail-2"
                ),
                default_duration_min=105,
                booking_channel=BookingChannel.WALK_IN,
            ),
        )
        getty = await repo.upsert_venue(
            session,
            VenueIn(
                name="Getty Villa",
                city="Pacific Palisades",
                venue_type=VenueType.MUSEUM,
                website="https://getty.example.com/visit/villa/",
                lat=34.0449819,
                lng=-118.5650197,
                default_duration_min=120,
                booking_channel=BookingChannel.TICKETED,
                platform_slug="https://tickets.getty.example.com/villa",
            ),
        )
        today = datetime.now(LA).date()
        past: dict[str, Any] = {}
        for key, title, venue, days_ago, rating in (
            ("loved_date_id", "Sushi at Nobu", nobu, 30, 10),
            ("meh_date_id", "Pasta at Bestia", bestia, 10, 6),
        ):
            day = today - timedelta(days=days_ago)
            planned = await repo.create_date(
                session, couple_id=couple.id, title=title, day=day
            )
            start = datetime.combine(day, time(19, 0), LA)
            await repo.add_stop(
                session,
                planned.id,
                StopIn(
                    title=title,
                    kind=StopKind.MEAL,
                    venue_id=venue.id,
                    start_at=start,
                    end_at=start + timedelta(hours=2),
                    est_duration_min=120,
                    anchor=Anchor.FIXED,
                    reservation_need=ReservationNeed.RECOMMENDED,
                    booking_status=StopBookingStatus.CONFIRMED,
                ),
            )
            await repo.rate_date(session, planned.id, "Alex", score=rating)
            past[key] = planned.id
        return {
            "couple_id": couple.id,
            "alex_id": alex.id,
            "sam_id": sam.id,
            "nobu_id": nobu.id,
            "bestia_id": bestia.id,
            "solstice_id": solstice.id,
            "getty_id": getty.id,
            **past,
        }
