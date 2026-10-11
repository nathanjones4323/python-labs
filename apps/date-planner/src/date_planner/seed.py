"""Put enough in the database to plan a date today.

    uv run --package date-planner python -m date_planner.seed

Idempotent: it upserts, so running it twice changes nothing. The couple is a
placeholder — edit it with `upsert_couple_profile` (or in psql) once the real
emails and shared calendar id are known.
"""

import asyncio
from datetime import time

from date_planner.booking.channels import BookingChannel, WindowUnit
from date_planner.db import open_session, repo
from date_planner.schemas import CoupleProfileIn, RestaurantIn

COUPLE = CoupleProfileIn(
    name="Nathan & partner",
    member_emails=[],
    shared_calendar_id=None,
    home_area="Santa Monica",
    timezone="America/Los_Angeles",
    budget_per_date=250,
    dietary=[],
    likes=["sushi", "ocean views", "wine bars", "live music"],
    dislikes=[],
    max_travel_minutes=45,
)

NOBU_MALIBU = RestaurantIn(
    name="Nobu Malibu",
    city="Malibu",
    address="22706 Pacific Coast Hwy, Malibu, CA 90265",
    phone="+13103179140",
    website="https://www.noburestaurants.com/malibu/home",
    timezone="America/Los_Angeles",
    booking_channel=BookingChannel.PHONE,
    # "One month to the exact calendar date", so months — not 30 days.
    window_amount=1,
    window_unit=WindowUnit.MONTHS,
    window_open_time=time(10, 0),
    notes=(
        "Credit card required; same-day cancel/no-show $50/pp. "
        "Sushi bar and lounge take walk-ins."
    ),
)


async def seed() -> None:
    async with open_session() as session:
        # `upsert_couple` keys on the id, so find this couple by name to
        # keep a second run from creating a second one.
        existing = [
            c for c in await repo.list_couples(session) if c.name == COUPLE.name
        ]
        profile = (
            COUPLE if not existing else COUPLE.model_copy(update={"id": existing[0].id})
        )
        couple = await repo.upsert_couple(session, profile)
        # Restaurants are keyed on (name, city), so this one upserts itself.
        nobu = await repo.upsert_restaurant(session, NOBU_MALIBU)
        print(f"couple     {couple.id}  {couple.name}")
        print(f"restaurant {nobu.id}  {nobu.name} ({nobu.booking_channel})")


def main() -> None:
    asyncio.run(seed())


if __name__ == "__main__":
    main()
