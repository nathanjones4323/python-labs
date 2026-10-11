"""The tools as a client sees them, over an in-memory MCP connection.

These go through FastMCP's own plumbing — schema validation on the way in,
serialisation on the way out — so they catch what a repo-level test can't:
a tool that takes an argument an agent can't express, or hands back something
that won't serialise.
"""

import contextlib
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import respx
from date_planner.server import create_server
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Factory = async_sessionmaker[AsyncSession]
LA = ZoneInfo("America/Los_Angeles")
NOBU_SITE = "https://nobu.example.com"


@contextlib.asynccontextmanager
async def client() -> AsyncIterator[Client[Any]]:
    async with Client(create_server()) as connected:
        yield connected


async def call(tool: str, /, **arguments: Any) -> Any:
    async with client() as connected:
        return (await connected.call_tool(tool, arguments)).data


async def test_ping_needs_no_database() -> None:
    assert await call("ping") == "pong"


async def test_every_tool_has_a_description() -> None:
    async with client() as connected:
        tools = await connected.list_tools()
    assert len(tools) == 14
    assert all(tool.description for tool in tools)


# --- couples ---------------------------------------------------------------


async def test_profile_round_trip(db: Factory, seeded: dict[str, Any]) -> None:
    profile = await call("get_couple_profile", couple_id=str(seeded["couple_id"]))
    assert profile.name == "Test couple"
    assert profile.budget_per_date == 250
    assert profile.dietary == ["no shellfish"]

    updated = await call(
        "upsert_couple_profile",
        profile={"id": str(seeded["couple_id"]), "budget_per_date": 180},
    )
    assert updated.budget_per_date == 180
    assert updated.dietary == ["no shellfish"]


async def test_a_new_profile_can_be_created_from_nothing(db: Factory) -> None:
    created = await call(
        "upsert_couple_profile",
        profile={"name": "New couple", "likes": ["tacos"]},
    )
    assert created.likes == ["tacos"]
    listed = await call("list_couples")
    assert [c.id for c in listed] == [created.id]


async def test_past_dates_come_back_with_ratings(
    db: Factory, seeded: dict[str, Any]
) -> None:
    past = await call("list_past_dates", couple_id=str(seeded["couple_id"]))
    assert [p.title for p in past] == ["Pasta at Bestia", "Sushi at Nobu"]
    assert [p.rating for p in past] == [3, 5]


async def test_an_unknown_couple_is_a_not_found_error(db: Factory) -> None:
    with pytest.raises(ToolError, match="NotFound"):
        await call(
            "get_couple_profile",
            couple_id="00000000-0000-0000-0000-000000000000",
        )


# --- restaurants -----------------------------------------------------------


async def test_find_then_upsert_a_restaurant(db: Factory) -> None:
    assert await call("find_restaurant", name="Kato", city="Los Angeles") is None
    created = await call(
        "upsert_restaurant",
        restaurant={
            "name": "Kato",
            "city": "Los Angeles",
            "booking_channel": "tock",
            "platform_slug": "kato",
        },
    )
    assert created.booking_channel == "tock"
    found = await call("find_restaurant", name="kato", city="los angeles")
    assert found.id == created.id


async def test_detection_is_cached_until_forced(
    db: Factory, seeded: dict[str, Any]
) -> None:
    html = """<html><body>
      <h2>Reservations</h2>
      <p>Reservations are accepted by phone, one month to the exact calendar
      date. Lines open at 10:00 am.</p>
      <a href="tel:+1-310-317-9140">Call us</a>
    </body></html>"""
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith=NOBU_SITE).respond(200, html=html)
        first = await call(
            "detect_booking_channel", restaurant_id=str(seeded["nobu_id"])
        )
        assert first.channel == "phone"
        assert first.from_cache is False
        fetched = len(router.calls)
        assert fetched > 0

        # The second ask is free.
        second = await call(
            "detect_booking_channel", restaurant_id=str(seeded["nobu_id"])
        )
        assert second.from_cache is True
        assert len(router.calls) == fetched

        # ... until it's forced.
        third = await call(
            "detect_booking_channel",
            restaurant_id=str(seeded["nobu_id"]),
            force=True,
        )
        assert third.from_cache is False
        assert len(router.calls) > fetched


async def test_detection_can_look_at_a_url_without_recording_it(
    db: Factory, seeded: dict[str, Any]
) -> None:
    html = (
        '<html><body><a href="https://www.exploretock.com/kato">Book</a></body></html>'
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://kato.example.com").respond(200, html=html)
        looked = await call(
            "detect_booking_channel", website_url="https://kato.example.com"
        )
    assert looked.channel == "tock"
    assert looked.platform_slug == "kato"
    assert looked.restaurant_id is None
    # Nothing was written: Nobu is still a phone venue with no slug.
    nobu = await call("find_restaurant", name="Nobu Malibu", city="Malibu")
    assert nobu.platform_slug is None


async def test_detection_needs_something_to_read(db: Factory) -> None:
    with pytest.raises(ToolError, match="DetectionFailed"):
        await call("detect_booking_channel")


async def test_booking_link_for_an_online_venue(
    db: Factory, seeded: dict[str, Any]
) -> None:
    link = await call(
        "build_booking_link",
        restaurant_id=str(seeded["bestia_id"]),
        # No offset: read as the restaurant's local time.
        start_at="2026-10-24T19:00:00",
        party_size=4,
    )
    assert link.url == (
        "https://resy.com/cities/los-angeles-ca/venues/bestia?date=2026-10-24&seats=4"
    )
    # Resy's template was verified 2026-10-10 against a different live venue
    # (A TÍ); this test's own "bestia" slug is a fixture, not a real one.
    assert link.verified is True
    assert link.local_start == datetime(2026, 10, 24, 19, 0, tzinfo=LA)


async def test_no_booking_link_for_a_phone_venue(
    db: Factory, seeded: dict[str, Any]
) -> None:
    with pytest.raises(ToolError, match="UnsupportedChannel"):
        await call(
            "build_booking_link",
            restaurant_id=str(seeded["nobu_id"]),
            start_at="2026-10-24T19:00:00",
        )


async def test_the_nobu_window_opens_a_calendar_month_before_at_ten(
    db: Factory, seeded: dict[str, Any]
) -> None:
    window = await call(
        "compute_booking_window",
        restaurant_id=str(seeded["nobu_id"]),
        target_start_at="2026-10-24T19:00:00",
        now="2026-09-01T12:00:00",
    )
    assert window.state == "not_yet_open"
    assert window.opens_at == datetime(2026, 9, 24, 10, 0, tzinfo=LA)
    assert window.assumed_open_time is False
    assert (window.window_amount, window.window_unit) == (1, "months")

    inside = await call(
        "compute_booking_window",
        restaurant_id=str(seeded["nobu_id"]),
        target_start_at="2026-10-24T19:00:00",
        now="2026-10-01T12:00:00",
    )
    assert inside.state == "open"


async def test_an_unrecorded_window_is_unknown(
    db: Factory, seeded: dict[str, Any]
) -> None:
    window = await call(
        "compute_booking_window",
        restaurant_id=str(seeded["bestia_id"]),
        target_start_at="2026-10-24T19:00:00",
    )
    assert window.state == "unknown"
    assert window.opens_at is None


# --- dates -----------------------------------------------------------------


async def test_planning_a_date_end_to_end(db: Factory, seeded: dict[str, Any]) -> None:
    created = await call(
        "create_date",
        couple_id=str(seeded["couple_id"]),
        title="Sunset dinner at Nobu Malibu",
        start_at="2026-10-24T19:00:00",
        end_at="2026-10-24T21:00:00",
        restaurant_id=str(seeded["nobu_id"]),
    )
    assert created.status == "proposed"
    assert created.start_at == datetime(2026, 10, 24, 19, 0, tzinfo=LA)

    pending = await call(
        "update_date",
        date_id=str(created.id),
        patch={
            "status": "booking_pending",
            "calendar_event_id": "evt_hold",
            "reminder_event_id": "evt_call",
        },
    )
    assert pending.status == "booking_pending"
    assert pending.reminder_event_id == "evt_call"

    confirmed = await call(
        "update_date",
        date_id=str(created.id),
        patch={"status": "confirmed", "confirmation_number": "NOBU-123"},
    )
    assert confirmed.confirmation_number == "NOBU-123"

    fetched = await call("get_date", date_id=str(created.id))
    assert fetched.status == "confirmed"

    upcoming = await call(
        "list_dates", couple_id=str(seeded["couple_id"]), status="confirmed"
    )
    assert [d.id for d in upcoming] == [created.id]


async def test_a_forbidden_status_move_is_reported_as_such(
    db: Factory, seeded: dict[str, Any]
) -> None:
    with pytest.raises(ToolError, match="InvalidTransition"):
        await call(
            "update_date",
            date_id=str(seeded["loved_date_id"]),
            patch={"status": "tentative"},
        )


async def test_list_dates_can_be_windowed(db: Factory, seeded: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    recent = await call(
        "list_dates",
        couple_id=str(seeded["couple_id"]),
        start_from=(now - timedelta(days=20)).isoformat(),
    )
    assert [d.id for d in recent] == [str(seeded["meh_date_id"])]
