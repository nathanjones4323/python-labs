"""The queries the tools run.

Every function takes an open `AsyncSession` and leaves committing to the
caller, so one tool call is one transaction. Lookups that an agent can
recover from raise `errors.NotFound` rather than returning None; the one
exception is `find_restaurant`, whose whole job is to answer "do we know this
place?".
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from date_planner.booking.detect import Detection
from date_planner.db.models import Couple, DateStatus, PlannedDate, Restaurant
from date_planner.errors import NotFound
from date_planner.schemas import CoupleProfileIn, DatePatch, RestaurantIn


def _assign(row: object, values: dict[str, object], required: frozenset[str]) -> None:
    """Write `values` onto `row`.

    An explicit null clears a field — that's how a preference gets removed —
    except for the columns in `required`, which have no sensible empty value
    and are left as they are.
    """
    for field, value in values.items():
        if value is None and field in required:
            continue
        setattr(row, field, value)


# --- couples ---------------------------------------------------------------


# Columns with no sensible empty value; see `_assign`.
COUPLE_REQUIRED = frozenset(
    {"name", "timezone", "member_emails", "dietary", "likes", "dislikes"}
)


async def get_couple(session: AsyncSession, couple_id: uuid.UUID) -> Couple:
    couple = await session.get(Couple, couple_id)
    if couple is None:
        raise NotFound(f"No couple {couple_id}")
    return couple


async def list_couples(session: AsyncSession) -> list[Couple]:
    result = await session.scalars(select(Couple).order_by(Couple.created_at))
    return list(result)


async def upsert_couple(session: AsyncSession, profile: CoupleProfileIn) -> Couple:
    """Update the couple `profile.id` names, or insert a new one."""
    values = profile.model_dump(exclude_unset=True, exclude={"id"})
    if profile.id is not None:
        couple = await get_couple(session, profile.id)
    else:
        if not values.get("name"):
            raise ValueError("A new couple needs a name")
        couple = Couple(name=values.pop("name"))
        session.add(couple)
    _assign(couple, values, COUPLE_REQUIRED)
    await session.flush()
    return couple


# --- restaurants -----------------------------------------------------------


RESTAURANT_REQUIRED = frozenset({"name", "city", "timezone", "booking_channel"})


async def get_restaurant(session: AsyncSession, restaurant_id: uuid.UUID) -> Restaurant:
    restaurant = await session.get(Restaurant, restaurant_id)
    if restaurant is None:
        raise NotFound(f"No restaurant {restaurant_id}")
    return restaurant


async def list_restaurants(session: AsyncSession) -> list[Restaurant]:
    result = await session.scalars(select(Restaurant).order_by(Restaurant.name))
    return list(result)


async def find_restaurant(
    session: AsyncSession, name: str, city: str
) -> Restaurant | None:
    """The venue with this name in this city, whatever the capitalisation."""
    found: Restaurant | None = await session.scalar(
        select(Restaurant).where(
            func.lower(Restaurant.name) == name.strip().lower(),
            func.lower(Restaurant.city) == city.strip().lower(),
        )
    )
    return found


async def upsert_restaurant(session: AsyncSession, data: RestaurantIn) -> Restaurant:
    """Update the venue `data.id` — or `(name, city)` — names, else insert it."""
    values = data.model_dump(exclude_unset=True, exclude={"id"})
    restaurant: Restaurant | None = None
    if data.id is not None:
        restaurant = await get_restaurant(session, data.id)
    elif data.name and data.city:
        restaurant = await find_restaurant(session, data.name, data.city)
    if restaurant is None:
        if not data.name or not data.city:
            raise ValueError("A new restaurant needs a name and a city")
        restaurant = Restaurant(name=data.name.strip(), city=data.city.strip())
        session.add(restaurant)
        values.pop("name", None)
        values.pop("city", None)
    _assign(restaurant, values, RESTAURANT_REQUIRED)
    await session.flush()
    return restaurant


async def record_detection(
    session: AsyncSession, restaurant: Restaurant, detection: Detection
) -> Restaurant:
    """Cache what detection found on the restaurant row.

    Only fills in a phone number or a booking window that wasn't there: a
    value a person typed in beats one a regex read off a web page.
    """
    restaurant.booking_channel = detection.channel
    restaurant.channel_confidence = detection.confidence
    restaurant.channel_evidence = detection.evidence
    restaurant.channel_checked_at = datetime.now(timezone.utc)
    if detection.platform_slug:
        restaurant.platform_slug = detection.platform_slug
    if detection.platform_city_slug:
        restaurant.platform_city_slug = detection.platform_city_slug
    if detection.phone and not restaurant.phone:
        restaurant.phone = detection.phone
    window = detection.window
    if window is not None and restaurant.window_amount is None:
        restaurant.window_amount = window.amount
        restaurant.window_unit = window.unit
        if window.open_time is not None and restaurant.window_open_time is None:
            restaurant.window_open_time = window.open_time
    await session.flush()
    return restaurant


# --- dates -----------------------------------------------------------------


DATE_REQUIRED = frozenset({"title", "start_at", "end_at", "party_size"})


async def get_date(session: AsyncSession, date_id: uuid.UUID) -> PlannedDate:
    planned = await session.get(PlannedDate, date_id)
    if planned is None:
        raise NotFound(f"No date {date_id}")
    return planned


async def create_date(
    session: AsyncSession,
    *,
    couple_id: uuid.UUID,
    title: str,
    start_at: datetime,
    end_at: datetime,
    restaurant_id: uuid.UUID | None = None,
    party_size: int = 2,
    notes: str | None = None,
) -> PlannedDate:
    await get_couple(session, couple_id)  # 404 now, not on a later update
    if restaurant_id is not None:
        await get_restaurant(session, restaurant_id)
    if end_at <= start_at:
        raise ValueError("end_at must be after start_at")
    planned = PlannedDate(
        couple_id=couple_id,
        restaurant_id=restaurant_id,
        title=title,
        start_at=start_at,
        end_at=end_at,
        party_size=party_size,
        notes=notes,
        status=DateStatus.PROPOSED,
    )
    session.add(planned)
    await session.flush()
    return planned


async def update_date(
    session: AsyncSession, date_id: uuid.UUID, patch: DatePatch
) -> PlannedDate:
    """Apply `patch`; a `status` in it goes through the state machine."""
    planned = await get_date(session, date_id)
    values = patch.model_dump(exclude_unset=True)
    status = values.pop("status", None)
    if values.get("restaurant_id") is not None:
        await get_restaurant(session, values["restaurant_id"])
    _assign(planned, values, DATE_REQUIRED)
    if planned.end_at <= planned.start_at:
        raise ValueError("end_at must be after start_at")
    if status is not None:
        planned.transition(DateStatus(status))
    await session.flush()
    return planned


async def list_dates(
    session: AsyncSession,
    couple_id: uuid.UUID,
    *,
    status: DateStatus | None = None,
    start_from: datetime | None = None,
    start_to: datetime | None = None,
    limit: int = 50,
) -> list[PlannedDate]:
    query = (
        select(PlannedDate)
        .where(PlannedDate.couple_id == couple_id)
        .order_by(PlannedDate.start_at.desc())
        .limit(limit)
    )
    if status is not None:
        query = query.where(PlannedDate.status == status)
    if start_from is not None:
        query = query.where(PlannedDate.start_at >= start_from)
    if start_to is not None:
        query = query.where(PlannedDate.start_at <= start_to)
    result = await session.scalars(query)
    return list(result)


async def list_past_dates(
    session: AsyncSession,
    couple_id: uuid.UUID,
    *,
    limit: int = 20,
    now: datetime | None = None,
) -> list[PlannedDate]:
    """Dates that have already started, most recent first.

    This is what keeps the skill from proposing last Friday's restaurant
    again, so cancelled dates are left out: they never happened.
    """
    now = now or datetime.now(timezone.utc)
    result = await session.scalars(
        select(PlannedDate)
        .where(
            PlannedDate.couple_id == couple_id,
            PlannedDate.start_at <= now,
            PlannedDate.status != DateStatus.CANCELLED,
        )
        .order_by(PlannedDate.start_at.desc())
        .limit(limit)
    )
    return list(result)
