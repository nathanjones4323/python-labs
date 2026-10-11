"""Print the deep links we'd generate, to open and check by hand.

    uv run --package date-planner python scripts/verify_links.py
    uv run --package date-planner python scripts/verify_links.py --from-db

Nothing here can tell whether a link prefills — only a human opening it can.
So: run this, open each link, and see whether the venue page comes up with
the date, time and party size already set. When one does, add its platform to
`links.VERIFIED` with today's date; `build_booking_link` then stops warning
about it.

The sample slugs are real venues, chosen because they're well known enough to
still exist. `--from-db` uses the restaurants on file instead.

A 404 or "not found" from the platform itself doesn't necessarily mean the
*template* is wrong — it can mean the sample slug is stale, or that the venue
isn't currently taking reservations on that platform at all. Check the
platform's own error message before concluding the URL shape needs fixing;
see the note above `links.VERIFIED` for the two cases that tripped this up.
"""

import argparse
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from date_planner.booking.channels import ONLINE_CHANNELS, BookingChannel
from date_planner.booking.links import VERIFIED, LinkTarget, build
from date_planner.db import open_session, repo
from date_planner.db.models import Restaurant
from date_planner.errors import DatePlannerError

LA = ZoneInfo("America/Los_Angeles")

SAMPLES: list[tuple[str, LinkTarget]] = [
    (
        "A TÍ (Resy, Los Angeles)",
        LinkTarget(
            channel=BookingChannel.RESY,
            timezone=LA,
            platform_slug="a-ti",
            platform_city_slug="los-angeles-ca",
        ),
    ),
    (
        "Bavel (OpenTable)",
        LinkTarget(
            channel=BookingChannel.OPENTABLE,
            timezone=LA,
            platform_slug="bavel-los-angeles",
        ),
    ),
    (
        # Kato itself isn't currently taking Tock reservations at all, which
        # made it a bad sample — Matsumoto is, as of 2026-10-10.
        "Matsumoto (Tock, Los Angeles)",
        LinkTarget(
            channel=BookingChannel.TOCK,
            timezone=LA,
            platform_slug="matsumoto-restaurant",
        ),
    ),
    (
        "Catch LA (SevenRooms)",
        LinkTarget(
            channel=BookingChannel.SEVENROOMS,
            timezone=LA,
            platform_slug="catchla",
        ),
    ),
]


def _targets_from_rows(rows: list[Restaurant]) -> list[tuple[str, LinkTarget]]:
    return [
        (f"{row.name} ({row.booking_channel}, {row.city})", row.link_target)
        for row in rows
        if row.booking_channel in ONLINE_CHANNELS
    ]


async def _from_db() -> list[tuple[str, LinkTarget]]:
    async with open_session() as session:
        return _targets_from_rows(await repo.list_restaurants(session))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-db",
        action="store_true",
        help="use the restaurants on file instead of the sample venues",
    )
    parser.add_argument(
        "--party-size", type=int, default=2, help="seats to ask for (default 2)"
    )
    args = parser.parse_args(argv)

    # A fortnight out: inside every platform's window, so the page should
    # show real availability rather than "not yet bookable".
    start = (datetime.now(LA) + timedelta(days=14)).replace(
        hour=19, minute=0, second=0, microsecond=0
    )
    targets = asyncio.run(_from_db()) if args.from_db else SAMPLES

    print(f"Party of {args.party_size} at {start:%A %d %B %Y, %-I:%M %p %Z}\n")
    for label, target in targets:
        try:
            link = build(target, start, args.party_size)
        except DatePlannerError as exc:
            print(f"{label}\n  skipped: {exc}\n")
            continue
        checked = VERIFIED.get(link.platform)
        state = f"verified {checked}" if checked else "UNVERIFIED"
        print(f"{label}  [{state}]\n  {link.url}\n")


if __name__ == "__main__":
    main()
