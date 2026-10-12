"""Put enough in the database to plan a date today.

    uv run --package date-planner python -m date_planner.seed

Idempotent: it upserts, so running it twice changes nothing. The couple's
emails and shared calendar id come from DATE_PLANNER_SEED_MEMBER_EMAILS
(comma-separated) and DATE_PLANNER_SHARED_CALENDAR_ID. A reseed with those
unset overwrites the stored values, so keep them in .env.

The venues are the Malibu day the multi-stop planner was built around: a
hike, a timed museum entry, a sunset dinner. Coordinates were looked up, not
guessed (2026-10-11):

* Solstice Canyon — the trailhead lot at Corral Canyon Rd, from HikeSpeak's
  trailhead point (34.03765, -118.7477), within a few hundred metres of the
  NPS figure for the same intersection.
* Getty Villa — OpenStreetMap's museum point (way 23014640).
* Nobu Malibu — OpenStreetMap's point for 22706 Pacific Coast Hwy.
"""

import asyncio
from datetime import time

from date_planner.booking.channels import BookingChannel, WindowUnit
from date_planner.config import get_settings
from date_planner.db import open_session, repo
from date_planner.db.models import VenueType
from date_planner.schemas import CoupleProfileIn, VenueIn


def build_couple() -> CoupleProfileIn:
    settings = get_settings()
    return CoupleProfileIn(
        name="Nathan & partner",
        member_emails=[
            e.strip() for e in settings.seed_member_emails.split(",") if e.strip()
        ],
        shared_calendar_id=settings.shared_calendar_id or None,
        home_area="Ventura, CA",
        timezone="America/Los_Angeles",
        budget_per_date=250,
        dietary=[],
        likes=["sushi", "ocean views", "live music", "scenic locations"],
        dislikes=[],
        max_travel_minutes=45,
    )


NOBU_MALIBU = VenueIn(
    name="Nobu Malibu",
    city="Malibu",
    venue_type=VenueType.RESTAURANT,
    lat=34.0389596,
    lng=-118.6696041,
    default_duration_min=90,
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

SOLSTICE_CANYON = VenueIn(
    name="Solstice Canyon",
    city="Malibu",
    venue_type=VenueType.TRAIL,
    address="Corral Canyon Rd & Solstice Canyon Rd, Malibu, CA 90265",
    timezone="America/Los_Angeles",
    lat=34.03765,
    lng=-118.7477,
    external_url="https://www.alltrails.com/trail/us/california/solstice-canyon-trail-2",
    # AllTrails: 2.9 mi loop, about 1.5 h; plus 15 min to park and get going.
    default_duration_min=105,
    booking_channel=BookingChannel.WALK_IN,
    notes="Small lot fills early on weekends; arrive early, bring water.",
)

GETTY_VILLA = VenueIn(
    name="Getty Villa",
    city="Pacific Palisades",
    venue_type=VenueType.MUSEUM,
    address="17985 Pacific Coast Hwy, Pacific Palisades, CA 90272",
    website="https://www.getty.edu/visit/villa/",
    timezone="America/Los_Angeles",
    lat=34.0449819,
    lng=-118.5650197,
    default_duration_min=120,
    booking_channel=BookingChannel.TICKETED,
    platform_slug="https://tickets.getty.edu/online/article/villa",
    notes="Free, but needs a timed-entry reservation; parking is paid.",
)


async def seed() -> None:
    couple_profile = build_couple()
    if couple_profile.shared_calendar_id is None:
        print("warning: DATE_PLANNER_SHARED_CALENDAR_ID is unset")
    async with open_session() as session:
        # `upsert_couple` keys on the id, so find this couple by name to
        # keep a second run from creating a second one.
        existing = [
            c for c in await repo.list_couples(session) if c.name == couple_profile.name
        ]
        profile = (
            couple_profile
            if not existing
            else couple_profile.model_copy(update={"id": existing[0].id})
        )
        couple = await repo.upsert_couple(session, profile)
        print(f"couple  {couple.id}  {couple.name}")
        # Venues are keyed on (name, city), so these upsert themselves.
        for data in (NOBU_MALIBU, SOLSTICE_CANYON, GETTY_VILLA):
            venue = await repo.upsert_venue(session, data)
            print(
                f"venue   {venue.id}  {venue.name} "
                f"({venue.venue_type}, {venue.booking_channel})"
            )


def main() -> None:
    asyncio.run(seed())


if __name__ == "__main__":
    main()
