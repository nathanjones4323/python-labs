"""Record the Malibu date Nathan and Jacqui went on, 2026-10-12, as completed.

    uv run --package date-planner python scripts/record_date_2026_10_12.py

An ad hoc date: it wasn't planned through the skill, so it's rebuilt here
from the events on Nathan's primary calendar (nathanjones4323@gmail.com) for
the day, read with the Google Calendar connector on 2026-10-12:

    11:30-12:40  Drive to Malibu + grab picnic lunch   (Vintage Grocers, Trancas)
    12:40-14:30  Picnic + Solstice Canyon hike
    14:30-15:00  Drive to Getty Villa
    15:00-16:30  Getty Villa (3pm timed entry)
    16:30-17:30  Santa Monica Pier
    18:15-19:15  Dinner At Neptune's Net

The drives are legs, not stops. The grocery stop has no event of its own:
the drive's description calls it "a quick 10-min stop", put here at 12:20,
about ten minutes' drive before the trailhead. Each stop keeps the id of
the calendar event it came from.

Run it after the seed. It also renames the seeded "Partner" member to
Jacqui (email left unset), and is safe to run twice: a date already
recorded for the day under this title is left alone. The new venues'
coordinates and Place IDs are from a Places Text Search on 2026-10-12.
"""

import asyncio
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from date_planner.booking.channels import BookingChannel
from date_planner.db import open_session, repo
from date_planner.db.models import (
    Anchor,
    ReservationNeed,
    StopBookingStatus,
    StopKind,
    VenueType,
)
from date_planner.schemas import DatePatch, MemberIn, StopIn, VenueIn
from date_planner.seed import _place, build_couple

LA = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 12)
TITLE = "Malibu: picnic hike, Getty Villa, Santa Monica Pier, Neptune's Net"

VINTAGE_GROCERS = VenueIn(
    name="Vintage Grocers",
    city="Malibu",
    venue_type=VenueType.OTHER,
    address="30745 Pacific Coast Hwy #24, Malibu, CA 90265",
    timezone="America/Los_Angeles",
    lat=34.031168,
    lng=-118.8424297,
    default_duration_min=10,
    booking_channel=BookingChannel.WALK_IN,
    notes="Grocery in the Trancas Country Market: sandwiches, drinks, snacks.",
)

SANTA_MONICA_PIER = VenueIn(
    name="Santa Monica Pier",
    city="Santa Monica",
    venue_type=VenueType.VIEWPOINT,
    address="200 Santa Monica Pier, Santa Monica, CA 90401",
    timezone="America/Los_Angeles",
    lat=34.0082821,
    lng=-118.4987585,
    default_duration_min=60,
    booking_channel=BookingChannel.WALK_IN,
)

NEPTUNES_NET = VenueIn(
    name="Neptune's Net",
    city="Malibu",
    venue_type=VenueType.RESTAURANT,
    address="42505 Pacific Coast Hwy, Malibu, CA 90265",
    timezone="America/Los_Angeles",
    lat=34.053197,
    lng=-118.962462,
    default_duration_min=60,
    booking_channel=BookingChannel.WALK_IN,
    notes="Casual seafood shack; order at the counter.",
)

PLACES = {
    "Vintage Grocers": _place(
        "ChIJy73keQ4Y6IARIjK_iFvmbOs",
        "Vintage Grocers",
        "Trancas Country Market, 30745 Pacific Coast Hwy #24, Malibu, CA 90265, USA",
        34.031168,
        -118.84242970000001,
    ),
    "Santa Monica Pier": _place(
        "ChIJm6deTdekwoARTY_RzhoRms0",
        "Santa Monica Pier",
        "200 Santa Monica Pier, Santa Monica, CA 90401, USA",
        34.008282099999995,
        -118.4987585,
    ),
    "Neptune's Net": _place(
        "ChIJeylkQ4Y-6IARyJW1rbrZg44",
        "Neptune's Net",
        "42505 CA-1, Malibu, CA 90265, USA",
        34.053197,
        -118.96246199999999,
    ),
}


def at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute), tzinfo=LA)


async def record() -> None:
    async with open_session() as session:
        name = build_couple().name
        couples = [c for c in await repo.list_couples(session) if c.name == name]
        if not couples:
            raise SystemExit(f"No couple {name!r}; run the seed first")
        couple = couples[0]

        names = {m.name.lower(): m for m in couple.members}
        if "jacqui" not in names and "partner" in names:
            await repo.upsert_member(
                session,
                couple.id,
                MemberIn(id=names["partner"].id, name="Jacqui", email=None),
            )
        else:
            await repo.upsert_member(session, couple.id, MemberIn(name="Jacqui"))
        print("member  Jacqui")

        existing = await repo.list_dates(session, couple.id, day_from=DAY, day_to=DAY)
        if any(d.title == TITLE for d in existing):
            print(f"date    already recorded for {DAY}")
            return

        venues = {}
        for venue_data in (VINTAGE_GROCERS, SANTA_MONICA_PIER, NEPTUNES_NET):
            venue = await repo.upsert_venue(session, venue_data)
            if not venue.google_place_id:
                await repo.apply_place(session, venue, PLACES[venue.name])
            venues[venue.name] = venue
            print(f"venue   {venue.id}  {venue.name}")
        for known in await repo.list_venues(session):
            if known.name in {"Solstice Canyon", "Getty Villa"}:
                venues[known.name] = known

        planned = await repo.create_date(
            session,
            couple_id=couple.id,
            title=TITLE,
            day=DAY,
            # Getty Villa -> Santa Monica Pier -> Neptune's Net is the long
            # leg home up PCH.
            max_leg_minutes=60,
            start_after=time(11, 30),
            notes=(
                "Left Ventura at 11:30 down PCH. Recorded after the fact from "
                "Nathan's calendar; dinner was Neptune's Net (Nobu was the "
                "other option)."
            ),
        )
        stops: list[StopIn] = [
            StopIn(
                title="Picnic lunch from Vintage Grocers",
                kind=StopKind.MEAL,
                venue_id=venues["Vintage Grocers"].id,
                start_at=at(12, 20),
                end_at=at(12, 30),
                reservation_need=ReservationNeed.NONE,
                calendar_event_id="40q9buljphfegso4c9l6oq1a4o",
                notes="Sandwiches, drinks, snacks for the trailhead picnic.",
            ),
            StopIn(
                title="Picnic + Solstice Canyon hike",
                kind=StopKind.ACTIVITY,
                venue_id=venues["Solstice Canyon"].id,
                start_at=at(12, 40),
                end_at=at(14, 30),
                reservation_need=ReservationNeed.NONE,
                calendar_event_id="h9hpalm6fiht25ojg79t8ive7k",
                notes=(
                    "Picnic at the trailhead tables, then the trail to the "
                    "ruins and waterfall (~2.6 mi round trip)."
                ),
            ),
            StopIn(
                title="Getty Villa (3pm timed entry)",
                kind=StopKind.TICKETED,
                venue_id=venues["Getty Villa"].id,
                start_at=at(15),
                end_at=at(16, 30),
                anchor=Anchor.FIXED,
                reservation_need=ReservationNeed.REQUIRED,
                booking_status=StopBookingStatus.CONFIRMED,
                calendar_event_id="v1472o17a3ila6d6j2p69fcbo4",
                notes="Gardens and reflecting pool. Parking $15 after 3pm.",
            ),
            StopIn(
                title="Santa Monica Pier",
                kind=StopKind.ACTIVITY,
                venue_id=venues["Santa Monica Pier"].id,
                start_at=at(16, 30),
                end_at=at(17, 30),
                reservation_need=ReservationNeed.NONE,
                calendar_event_id="037rm9bqos0g6enn7573q0a81k",
            ),
            StopIn(
                title="Dinner at Neptune's Net",
                kind=StopKind.MEAL,
                venue_id=venues["Neptune's Net"].id,
                start_at=at(18, 15),
                end_at=at(19, 15),
                reservation_need=ReservationNeed.NONE,
                calendar_event_id="jfarmjclc7b152rddm6a6nhb2o",
            ),
        ]
        for data in stops:
            await repo.add_stop(session, planned.id, data)
        planned = await repo.update_date(
            session, planned.id, DatePatch(status="completed")
        )
        print(f"date    {planned.id}  {planned.title} ({planned.status})")
        for stop in planned.stops:
            print(f"  stop  {stop.position}  {stop.title}")


def main() -> None:
    asyncio.run(record())


if __name__ == "__main__":
    main()
