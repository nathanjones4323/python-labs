"""Migration 002 against real v1 data: up to v2, check it, back down, compare.

Synchronous on purpose: Alembic's env.py runs its own event loop, which it
can't do inside an async test's.
"""

import asyncio
from typing import Any

from alembic import command
from conftest import alembic_config
from date_planner.db import make_engine
from sqlalchemy import text

COUPLE = "00000000-0000-0000-0000-00000000c001"
NOBU = "00000000-0000-0000-0000-0000000000a1"  # phone
CAFE = "00000000-0000-0000-0000-0000000000a2"  # walk-in
BESTIA = "00000000-0000-0000-0000-0000000000a3"  # resy

V1_ROWS = [
    f"""INSERT INTO couples (id, name, member_emails, timezone, dietary, likes,
        dislikes) VALUES ('{COUPLE}', 'A & B', '{{a@x.com}}', 'America/Los_Angeles',
        '{{}}', '{{sushi}}', '{{}}')""",
    f"""INSERT INTO restaurants (id, name, city, timezone, booking_channel,
        phone, window_amount, window_unit, window_open_time) VALUES
        ('{NOBU}', 'Nobu Malibu', 'Malibu', 'America/Los_Angeles', 'phone',
         '+13103179140', 1, 'months', '10:00')""",
    f"""INSERT INTO restaurants (id, name, city, timezone, booking_channel)
        VALUES ('{CAFE}', 'Cafe', 'Venice', 'America/Los_Angeles', 'walk_in')""",
    f"""INSERT INTO restaurants (id, name, city, timezone, booking_channel,
        platform_slug, platform_city_slug) VALUES ('{BESTIA}', 'Bestia',
        'Los Angeles', 'America/Los_Angeles', 'resy', 'bestia', 'los-angeles-ca')""",
]

# (id suffix, restaurant, status, calendar id, reminder id, booking url,
#  confirmation, rating). Every v1 status, and each booking_pending flavour.
DATES = [
    ("d1", NOBU, "proposed", None, None, None, None, None),
    ("d2", NOBU, "tentative", "evt_hold", None, None, None, None),
    ("d3", NOBU, "booking_pending", "evt_hold", "evt_call", None, None, None),
    (
        "d4",
        BESTIA,
        "booking_pending",
        "evt_hold",
        None,
        "https://resy.com/x",
        None,
        None,
    ),
    ("d5", BESTIA, "confirmed", "evt_hold", None, "https://resy.com/x", "R-1", None),
    ("d6", NOBU, "completed", "evt_hold", None, None, "N-1", 5),
    ("d7", BESTIA, "cancelled", "evt_hold", None, None, None, None),
    ("d8", CAFE, "proposed", None, None, None, None, None),
    ("d9", None, "tentative", "evt_beach", None, None, None, None),
]


def _date_id(suffix: str) -> str:
    return f"00000000-0000-0000-0000-0000000000{suffix}"


def _insert_date(row: tuple[Any, ...]) -> str:
    suffix, restaurant, status, cal, reminder, url, conf, rating = row

    def lit(value: Any) -> str:
        return "NULL" if value is None else f"'{value}'"

    # 02:00 UTC on the 24th is 19:00 on the 23rd in Los Angeles: the day
    # has to come out as the 23rd.
    return f"""INSERT INTO dates (id, couple_id, restaurant_id, title, start_at,
        end_at, party_size, status, calendar_event_id, reminder_event_id,
        booking_url, confirmation_number, rating, notes) VALUES
        ('{_date_id(suffix)}', '{COUPLE}', {lit(restaurant)}, 'Date {suffix}',
         '2026-10-24T02:00:00Z', '2026-10-24T04:00:00Z', 2, '{status}',
         {lit(cal)}, {lit(reminder)}, {lit(url)}, {lit(conf)}, {lit(rating)},
         'note {suffix}')"""


def _run(
    url: str, statements: list[str], query: str | None = None
) -> list[dict[str, Any]]:
    async def go() -> list[dict[str, Any]]:
        engine = make_engine(url)
        try:
            async with engine.begin() as conn:
                for statement in statements:
                    await conn.execute(text(statement))
                if query is None:
                    return []
                result = await conn.execute(text(query))
                return [dict(row._mapping) for row in result]
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _snapshot(url: str) -> dict[str, list[dict[str, Any]]]:
    return {
        table: _run(url, [], f"SELECT * FROM {table} ORDER BY id")
        for table in ("couples", "restaurants", "dates")
    }


def test_v1_data_survives_the_round_trip(migrated: str) -> None:
    config = alembic_config(migrated)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "001")
        _run(migrated, V1_ROWS + [_insert_date(row) for row in DATES])
        before = _snapshot(migrated)

        command.upgrade(config, "002")
        stops = {
            str(row["date_id"])[-2:]: row
            for row in _run(
                migrated,
                [],
                """SELECT s.*, d.status AS date_status, d.day
                   FROM date_stops s JOIN dates d ON d.id = s.date_id""",
            )
        }
        assert len(stops) == len(DATES)
        expected = {
            # suffix: (stop booking_status, date status, kind, anchor, need)
            "d1": ("needed", "planning", "meal", "fixed", "recommended"),
            "d2": ("needed", "planning", "meal", "fixed", "recommended"),
            "d3": ("reminder_set", "booking_pending", "meal", "fixed", "recommended"),
            "d4": ("link_sent", "booking_pending", "meal", "fixed", "recommended"),
            "d5": ("confirmed", "confirmed", "meal", "fixed", "recommended"),
            "d6": ("confirmed", "completed", "meal", "fixed", "recommended"),
            "d7": ("cancelled", "cancelled", "meal", "fixed", "recommended"),
            "d8": ("not_needed", "planning", "meal", "none", "none"),
            "d9": ("not_needed", "planning", "activity", "none", "unknown"),
        }
        for suffix, (booking, status, kind, anchor, need) in expected.items():
            stop = stops[suffix]
            assert (
                stop["booking_status"],
                stop["date_status"],
                stop["kind"],
                stop["anchor"],
                stop["reservation_need"],
            ) == (booking, status, kind, anchor, need), suffix
            assert stop["position"] == 0
            assert stop["est_duration_min"] == 120
            assert str(stop["day"]) == "2026-10-23"
            assert stop["title"] == f"Date {suffix}"
        assert stops["d3"]["reminder_event_id"] == "evt_call"
        assert stops["d5"]["confirmation_number"] == "R-1"
        venues = _run(migrated, [], "SELECT venue_type FROM venues")
        assert {v["venue_type"] for v in venues} == {"restaurant"}

        command.downgrade(config, "001")
        after = _snapshot(migrated)
        assert after == before
    finally:
        # Leave the schema where the rest of the suite expects it.
        command.upgrade(config, "head")


def test_downgrade_keeps_the_first_stop_of_a_v2_date(migrated: str) -> None:
    """A v2-only date still comes down as a valid v1 row."""
    config = alembic_config(migrated)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        venue = "00000000-0000-0000-0000-0000000000b1"
        _run(
            migrated,
            [
                V1_ROWS[0],
                f"""INSERT INTO venues (id, name, city, venue_type, timezone,
                    booking_channel) VALUES ('{venue}', 'Getty Villa',
                    'Pacific Palisades', 'museum', 'America/Los_Angeles',
                    'ticketed')""",
                f"""INSERT INTO dates (id, couple_id, title, day, max_leg_minutes,
                    status) VALUES ('{_date_id("e1")}', '{COUPLE}', 'Malibu day',
                    '2026-10-24', 45, 'planning')""",
                f"""INSERT INTO date_stops (id, date_id, position, title, kind,
                    venue_id, party_size, anchor, sunset_offset_min,
                    reservation_need, booking_status, travel_mode) VALUES
                    (gen_random_uuid(), '{_date_id("e1")}', 0, 'Getty', 'ticketed',
                     '{venue}', 2, 'fixed', -30, 'required', 'needed', 'driving'),
                    (gen_random_uuid(), '{_date_id("e1")}', 1, 'Dinner', 'meal',
                     NULL, 2, 'sunset', -30, 'unknown', 'needed', 'driving')""",
                # A date with no stops at all.
                f"""INSERT INTO dates (id, couple_id, title, day, max_leg_minutes,
                    status) VALUES ('{_date_id("e2")}', '{COUPLE}', 'Empty',
                    '2026-10-25', 25, 'planning')""",
            ],
        )
        command.downgrade(config, "001")
        rows = {
            str(r["id"])[-2:]: r
            for r in _run(
                migrated,
                [],
                """SELECT d.*, r.booking_channel FROM dates d
                   LEFT JOIN restaurants r ON r.id = d.restaurant_id""",
            )
        }
        getty = rows["e1"]
        assert str(getty["restaurant_id"]) == venue
        # ticketed doesn't exist in v1.
        assert getty["booking_channel"] == "unknown"
        assert getty["status"] == "proposed"
        # No time on the stop: noon local on the day, two hours long.
        assert getty["start_at"].isoformat() == "2026-10-24T19:00:00+00:00"
        assert (getty["end_at"] - getty["start_at"]).seconds == 7200
        assert rows["e2"]["start_at"].isoformat() == "2026-10-25T19:00:00+00:00"
    finally:
        command.upgrade(config, "head")
