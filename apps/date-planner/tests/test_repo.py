"""The repo layer against a real Postgres: upserts, transitions, listings."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from date_planner.booking.channels import BookingChannel, WindowUnit
from date_planner.booking.detect import Detection
from date_planner.booking.window import BookingWindow
from date_planner.db import repo
from date_planner.db.models import DATE_TRANSITIONS, DateStatus
from date_planner.errors import InvalidTransition, NotFound
from date_planner.schemas import CoupleProfileIn, DatePatch, RestaurantIn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Factory = async_sessionmaker[AsyncSession]


# --- couples ---------------------------------------------------------------


async def test_upsert_couple_inserts_then_updates(db: Factory) -> None:
    async with db() as session, session.begin():
        couple = await repo.upsert_couple(
            session, CoupleProfileIn(name="A & B", likes=["sushi"])
        )
        couple_id = couple.id
    async with db() as session, session.begin():
        again = await repo.upsert_couple(
            session, CoupleProfileIn(id=couple_id, budget_per_date=300)
        )
        assert again.id == couple_id
        # The preference it didn't mention is still there.
        assert again.likes == ["sushi"]
        assert again.budget_per_date == 300
        assert len(await repo.list_couples(session)) == 1


async def test_a_new_couple_needs_a_name(db: Factory) -> None:
    async with db() as session, session.begin():
        with pytest.raises(ValueError, match="needs a name"):
            await repo.upsert_couple(session, CoupleProfileIn(likes=["sushi"]))


async def test_an_explicit_null_clears_an_optional_field(db: Factory) -> None:
    async with db() as session, session.begin():
        couple = await repo.upsert_couple(
            session, CoupleProfileIn(name="A & B", budget_per_date=300)
        )
        cleared = await repo.upsert_couple(
            session, CoupleProfileIn(id=couple.id, budget_per_date=None)
        )
        assert cleared.budget_per_date is None


async def test_a_null_name_is_ignored_rather_than_breaking_the_row(
    db: Factory,
) -> None:
    async with db() as session, session.begin():
        couple = await repo.upsert_couple(session, CoupleProfileIn(name="A & B"))
        same = await repo.upsert_couple(
            session, CoupleProfileIn(id=couple.id, name=None)
        )
        assert same.name == "A & B"


async def test_unknown_couple_is_not_found(db: Factory) -> None:
    async with db() as session, session.begin():
        with pytest.raises(NotFound):
            await repo.get_couple(session, uuid.uuid4())


# --- restaurants -----------------------------------------------------------


async def test_restaurants_are_keyed_on_name_and_city(db: Factory) -> None:
    async with db() as session, session.begin():
        first = await repo.upsert_restaurant(
            session, RestaurantIn(name="Nobu Malibu", city="Malibu")
        )
        second = await repo.upsert_restaurant(
            session,
            RestaurantIn(name="nobu malibu", city="MALIBU", phone="+13103179140"),
        )
        assert second.id == first.id
        assert second.phone == "+13103179140"
        assert len(await repo.list_restaurants(session)) == 1


async def test_find_restaurant_ignores_case_and_padding(db: Factory) -> None:
    async with db() as session, session.begin():
        await repo.upsert_restaurant(
            session, RestaurantIn(name="Bestia", city="Los Angeles")
        )
        assert await repo.find_restaurant(session, " bestia ", "los angeles")
        assert await repo.find_restaurant(session, "Bestia", "Malibu") is None


async def test_a_new_restaurant_needs_a_name_and_city(db: Factory) -> None:
    async with db() as session, session.begin():
        with pytest.raises(ValueError, match="name and a city"):
            await repo.upsert_restaurant(session, RestaurantIn(name="Bestia"))


async def test_record_detection_caches_channel_and_window(db: Factory) -> None:
    async with db() as session, session.begin():
        restaurant = await repo.upsert_restaurant(
            session, RestaurantIn(name="Bestia", city="Los Angeles")
        )
        await repo.record_detection(
            session,
            restaurant,
            Detection(
                channel=BookingChannel.RESY,
                confidence=0.95,
                evidence="resy link",
                platform_slug="bestia",
                platform_city_slug="los-angeles-ca",
                phone="+12135550000",
                window=BookingWindow(30, WindowUnit.DAYS),
            ),
        )
        assert restaurant.booking_channel is BookingChannel.RESY
        assert restaurant.platform_slug == "bestia"
        assert restaurant.channel_checked_at is not None
        assert restaurant.window_amount == 30
        assert restaurant.window_unit is WindowUnit.DAYS
        assert restaurant.phone == "+12135550000"


async def test_detection_does_not_overwrite_what_a_person_entered(
    db: Factory,
) -> None:
    async with db() as session, session.begin():
        restaurant = await repo.upsert_restaurant(
            session,
            RestaurantIn(
                name="Nobu Malibu",
                city="Malibu",
                phone="+13103179140",
                window_amount=1,
                window_unit=WindowUnit.MONTHS,
            ),
        )
        await repo.record_detection(
            session,
            restaurant,
            Detection(
                channel=BookingChannel.PHONE,
                confidence=0.8,
                evidence="calls only",
                phone="+10000000000",
                window=BookingWindow(30, WindowUnit.DAYS),
            ),
        )
        assert restaurant.phone == "+13103179140"
        assert restaurant.window_amount == 1
        assert restaurant.window_unit is WindowUnit.MONTHS


# --- dates -----------------------------------------------------------------


async def test_create_date_starts_proposed(db: Factory, seeded: dict[str, Any]) -> None:
    async with db() as session, session.begin():
        start = datetime.now(timezone.utc) + timedelta(days=14)
        planned = await repo.create_date(
            session,
            couple_id=seeded["couple_id"],
            title="Dinner at Nobu",
            start_at=start,
            end_at=start + timedelta(hours=2),
            restaurant_id=seeded["nobu_id"],
        )
        assert planned.status is DateStatus.PROPOSED
        assert planned.party_size == 2


async def test_create_date_rejects_an_end_before_its_start(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        start = datetime.now(timezone.utc) + timedelta(days=14)
        with pytest.raises(ValueError, match="after start_at"):
            await repo.create_date(
                session,
                couple_id=seeded["couple_id"],
                title="Backwards",
                start_at=start,
                end_at=start - timedelta(hours=1),
            )


async def test_create_date_checks_the_couple_and_restaurant_exist(
    db: Factory, seeded: dict[str, Any]
) -> None:
    start = datetime.now(timezone.utc) + timedelta(days=14)
    async with db() as session, session.begin():
        with pytest.raises(NotFound):
            await repo.create_date(
                session,
                couple_id=uuid.uuid4(),
                title="Nobody's date",
                start_at=start,
                end_at=start + timedelta(hours=2),
            )
    async with db() as session, session.begin():
        with pytest.raises(NotFound):
            await repo.create_date(
                session,
                couple_id=seeded["couple_id"],
                title="Nowhere",
                start_at=start,
                end_at=start + timedelta(hours=2),
                restaurant_id=uuid.uuid4(),
            )


async def test_the_booking_path_walks_the_state_machine(
    db: Factory, seeded: dict[str, Any]
) -> None:
    start = datetime.now(timezone.utc) + timedelta(days=14)
    async with db() as session, session.begin():
        planned = await repo.create_date(
            session,
            couple_id=seeded["couple_id"],
            title="Dinner at Bestia",
            start_at=start,
            end_at=start + timedelta(hours=2),
            restaurant_id=seeded["bestia_id"],
        )
        date_id = planned.id
    async with db() as session, session.begin():
        held = await repo.update_date(
            session,
            date_id,
            DatePatch(
                status=DateStatus.BOOKING_PENDING,
                calendar_event_id="evt_1",
                booking_url="https://resy.com/...",
            ),
        )
        assert held.status is DateStatus.BOOKING_PENDING
        assert held.calendar_event_id == "evt_1"
        confirmed = await repo.update_date(
            session,
            date_id,
            DatePatch(status=DateStatus.CONFIRMED, confirmation_number="ABC123"),
        )
        assert confirmed.status is DateStatus.CONFIRMED
        done = await repo.update_date(
            session, date_id, DatePatch(status=DateStatus.COMPLETED, rating=5)
        )
        assert done.status is DateStatus.COMPLETED
        assert done.rating == 5


async def test_a_forbidden_move_is_refused(db: Factory, seeded: dict[str, Any]) -> None:
    start = datetime.now(timezone.utc) + timedelta(days=14)
    async with db() as session, session.begin():
        planned = await repo.create_date(
            session,
            couple_id=seeded["couple_id"],
            title="Dinner",
            start_at=start,
            end_at=start + timedelta(hours=2),
        )
        # Nothing has been booked, so it can't be confirmed.
        with pytest.raises(InvalidTransition):
            await repo.update_date(
                session, planned.id, DatePatch(status=DateStatus.CONFIRMED)
            )


async def test_a_completed_date_is_the_end_of_the_line(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        with pytest.raises(InvalidTransition):
            await repo.update_date(
                session,
                seeded["loved_date_id"],
                DatePatch(status=DateStatus.TENTATIVE),
            )


async def test_setting_the_same_status_is_not_an_error(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        same = await repo.update_date(
            session,
            seeded["loved_date_id"],
            DatePatch(status=DateStatus.COMPLETED, notes="still great"),
        )
        assert same.notes == "still great"


def test_every_status_has_a_transition_rule() -> None:
    assert set(DATE_TRANSITIONS) == set(DateStatus)


async def test_list_past_dates_is_newest_first_and_skips_cancelled(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        now = datetime.now(timezone.utc)
        past = await repo.list_past_dates(session, seeded["couple_id"])
        assert [p.id for p in past] == [
            seeded["meh_date_id"],
            seeded["loved_date_id"],
        ]
        assert [p.rating for p in past] == [3, 5]
        # A future date isn't a past date.
        start = now + timedelta(days=3)
        await repo.create_date(
            session,
            couple_id=seeded["couple_id"],
            title="Upcoming",
            start_at=start,
            end_at=start + timedelta(hours=2),
        )
        assert len(await repo.list_past_dates(session, seeded["couple_id"])) == 2
        # A date that was cancelled before it happened never happened.
        flaked_start = now - timedelta(days=2)
        flaked = await repo.create_date(
            session,
            couple_id=seeded["couple_id"],
            title="Cancelled on us",
            start_at=flaked_start,
            end_at=flaked_start + timedelta(hours=2),
        )
        await repo.update_date(
            session, flaked.id, DatePatch(status=DateStatus.CANCELLED)
        )
        after = await repo.list_past_dates(session, seeded["couple_id"])
        assert [p.id for p in after] == [
            seeded["meh_date_id"],
            seeded["loved_date_id"],
        ]


async def test_list_dates_filters_by_status_and_window(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        now = datetime.now(timezone.utc)
        completed = await repo.list_dates(
            session, seeded["couple_id"], status=DateStatus.COMPLETED
        )
        assert len(completed) == 2
        recent = await repo.list_dates(
            session, seeded["couple_id"], start_from=now - timedelta(days=20)
        )
        assert [r.id for r in recent] == [seeded["meh_date_id"]]
        nothing = await repo.list_dates(
            session, seeded["couple_id"], start_to=now - timedelta(days=365)
        )
        assert nothing == []
