"""The tools as a client sees them, over an in-memory MCP connection.

These go through FastMCP's own plumbing — schema validation on the way in,
serialisation on the way out — so they catch what a repo-level test can't:
a tool that takes an argument an agent can't express, or hands back something
that won't serialise.
"""

import contextlib
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import respx
from date_planner.db import repo
from date_planner.server import create_server
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Factory = async_sessionmaker[AsyncSession]
LA = ZoneInfo("America/Los_Angeles")
NOBU_SITE = "https://nobu.example.com"
DAY = "2026-10-24"


@contextlib.asynccontextmanager
async def client() -> AsyncIterator[Client[Any]]:
    async with Client(create_server()) as connected:
        yield connected


async def call(tool: str, /, **arguments: Any) -> Any:
    async with client() as connected:
        return (await connected.call_tool(tool, arguments)).data


async def new_date(seeded: dict[str, Any], **extra: Any) -> Any:
    return await call(
        "create_date",
        couple_id=str(seeded["couple_id"]),
        title="Malibu day",
        day=DAY,
        **extra,
    )


async def add(date_id: Any, position: int | None = None, **stop: Any) -> Any:
    return await call("add_stop", date_id=str(date_id), stop=stop, position=position)


async def test_ping_needs_no_database() -> None:
    assert await call("ping") == "pong"


async def test_every_tool_has_a_description() -> None:
    async with client() as connected:
        tools = await connected.list_tools()
    assert len(tools) == 23
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


async def test_preferences_can_be_edited_an_item_at_a_time(
    db: Factory, seeded: dict[str, Any]
) -> None:
    edited = await call(
        "edit_couple_preferences",
        couple_id=str(seeded["couple_id"]),
        edit={"add_likes": ["tapas"], "add_dislikes": ["sushi"]},
    )
    assert edited.likes == ["tapas"]
    assert edited.dislikes == ["loud rooms", "sushi"]
    assert edited.dietary == ["no shellfish"]


async def test_past_dates_come_back_with_ratings_and_stops(
    db: Factory, seeded: dict[str, Any]
) -> None:
    past = await call("list_past_dates", couple_id=str(seeded["couple_id"]))
    assert [p.title for p in past] == ["Pasta at Bestia", "Sushi at Nobu"]
    assert [p.rating for p in past] == [3, 5]
    assert [p.stops[0].venue_name for p in past] == ["Bestia", "Nobu Malibu"]


async def test_an_unknown_couple_is_a_not_found_error(db: Factory) -> None:
    with pytest.raises(ToolError, match="NotFound"):
        await call(
            "get_couple_profile",
            couple_id="00000000-0000-0000-0000-000000000000",
        )


# --- venues ----------------------------------------------------------------


async def test_find_then_upsert_a_venue(db: Factory) -> None:
    assert await call("find_venue", name="Kato", city="Los Angeles") is None
    created = await call(
        "upsert_venue",
        venue={
            "name": "Kato",
            "city": "Los Angeles",
            "venue_type": "restaurant",
            "booking_channel": "tock",
            "platform_slug": "kato",
        },
    )
    assert created.booking_channel == "tock"
    found = await call("find_venue", name="kato", city="los angeles")
    assert found.id == created.id


async def test_a_new_venue_without_a_type_is_refused(db: Factory) -> None:
    with pytest.raises(ToolError, match="venue_type"):
        await call("upsert_venue", venue={"name": "Kato", "city": "Los Angeles"})


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
        first = await call("detect_booking_channel", venue_id=str(seeded["nobu_id"]))
        assert first.channel == "phone"
        assert first.from_cache is False
        fetched = len(router.calls)
        assert fetched > 0

        # The second ask is free.
        second = await call("detect_booking_channel", venue_id=str(seeded["nobu_id"]))
        assert second.from_cache is True
        assert len(router.calls) == fetched

        # ... until it's forced.
        third = await call(
            "detect_booking_channel",
            venue_id=str(seeded["nobu_id"]),
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
    assert looked.venue_id is None
    # Nothing was written: Nobu is still a phone venue with no slug.
    nobu = await call("find_venue", name="Nobu Malibu", city="Malibu")
    assert nobu.platform_slug is None


async def test_detection_needs_something_to_read(db: Factory) -> None:
    with pytest.raises(ToolError, match="DetectionFailed"):
        await call("detect_booking_channel")


# --- booking, per stop -----------------------------------------------------


async def test_booking_link_for_an_online_venue(
    db: Factory, seeded: dict[str, Any]
) -> None:
    planned = await new_date(seeded)
    planned = await add(
        planned.id,
        title="Dinner at Bestia",
        kind="meal",
        venue_id=str(seeded["bestia_id"]),
        party_size=4,
        # No offset: read as the couple's local time.
        start_at="2026-10-24T19:00:00",
    )
    link = await call("build_booking_link", stop_id=str(planned.stops[0].id))
    assert link.url == (
        "https://resy.com/cities/los-angeles-ca/venues/bestia?date=2026-10-24&seats=4"
    )
    # Resy's template was verified 2026-10-10 against a different live venue
    # (A TÍ); this test's own "bestia" slug is a fixture, not a real one.
    assert link.verified is True
    assert link.local_start == datetime(2026, 10, 24, 19, 0, tzinfo=LA)


async def test_a_ticketed_link_is_the_venues_own_page(
    db: Factory, seeded: dict[str, Any]
) -> None:
    planned = await new_date(seeded)
    planned = await add(
        planned.id,
        title="Getty Villa",
        kind="ticketed",
        venue_id=str(seeded["getty_id"]),
        start_at="2026-10-24T13:00:00",
    )
    link = await call("build_booking_link", stop_id=str(planned.stops[0].id))
    assert link.url == "https://tickets.getty.example.com/villa"
    assert link.platform == "ticketed"
    assert link.verified is False


async def test_no_booking_link_for_a_phone_venue(
    db: Factory, seeded: dict[str, Any]
) -> None:
    planned = await new_date(seeded)
    planned = await add(
        planned.id,
        title="Nobu",
        kind="meal",
        venue_id=str(seeded["nobu_id"]),
        start_at="2026-10-24T19:00:00",
    )
    with pytest.raises(ToolError, match="UnsupportedChannel"):
        await call("build_booking_link", stop_id=str(planned.stops[0].id))


async def test_a_link_needs_a_venue_and_a_time(
    db: Factory, seeded: dict[str, Any]
) -> None:
    planned = await new_date(seeded)
    planned = await add(planned.id, title="Sunset dinner", kind="meal")
    with pytest.raises(ToolError, match="IncompleteStop: .*no venue"):
        await call("build_booking_link", stop_id=str(planned.stops[0].id))
    planned = await call(
        "update_stop",
        stop_id=str(planned.stops[0].id),
        patch={"venue_id": str(seeded["bestia_id"])},
    )
    with pytest.raises(ToolError, match="IncompleteStop: .*no start_at"):
        await call("build_booking_link", stop_id=str(planned.stops[0].id))


async def test_the_nobu_window_opens_a_calendar_month_before_at_ten(
    db: Factory, seeded: dict[str, Any]
) -> None:
    planned = await new_date(seeded)
    planned = await add(
        planned.id,
        title="Nobu",
        kind="meal",
        venue_id=str(seeded["nobu_id"]),
        start_at="2026-10-24T19:00:00",
    )
    stop_id = str(planned.stops[0].id)
    window = await call(
        "compute_booking_window", stop_id=stop_id, now="2026-09-01T12:00:00"
    )
    assert window.state == "not_yet_open"
    assert window.opens_at == datetime(2026, 9, 24, 10, 0, tzinfo=LA)
    assert window.assumed_open_time is False
    assert (window.window_amount, window.window_unit) == (1, "months")

    inside = await call(
        "compute_booking_window", stop_id=stop_id, now="2026-10-01T12:00:00"
    )
    assert inside.state == "open"


async def test_an_unrecorded_window_is_unknown(
    db: Factory, seeded: dict[str, Any]
) -> None:
    planned = await new_date(seeded)
    planned = await add(
        planned.id,
        title="Bestia",
        kind="meal",
        venue_id=str(seeded["bestia_id"]),
        start_at="2026-10-24T19:00:00",
    )
    window = await call("compute_booking_window", stop_id=str(planned.stops[0].id))
    assert window.state == "unknown"
    assert window.opens_at is None


@pytest.mark.parametrize(
    ("venue", "evidence", "need"),
    [
        ("solstice_id", None, "none"),
        ("getty_id", None, "required"),
        ("nobu_id", None, "recommended"),
        ("bestia_id", 'Resy link; reservations required: "required"', "required"),
    ],
)
async def test_reservation_need_follows_the_channel(
    db: Factory, seeded: dict[str, Any], venue: str, evidence: str | None, need: str
) -> None:
    if evidence:
        # Only detection writes channel_evidence; stand in for it.
        async with db() as session, session.begin():
            row = await repo.get_venue(session, seeded[venue])
            row.channel_evidence = evidence
    planned = await new_date(seeded)
    planned = await add(
        planned.id, title="Stop", kind="meal", venue_id=str(seeded[venue])
    )
    suggestion = await call(
        "suggest_reservation_need", stop_id=str(planned.stops[0].id)
    )
    assert suggestion.suggested == need


@pytest.mark.parametrize(("party_size", "need"), [(2, "recommended"), (6, "required")])
async def test_a_large_party_rule_only_binds_a_large_party(
    db: Factory, seeded: dict[str, Any], party_size: int, need: str
) -> None:
    async with db() as session, session.begin():
        row = await repo.get_venue(session, seeded["bestia_id"])
        row.channel_evidence = (
            'Resy link; reservations required for parties of 6+: "required"'
        )
    planned = await new_date(seeded)
    planned = await add(
        planned.id,
        title="Pasta",
        kind="meal",
        venue_id=str(seeded["bestia_id"]),
        party_size=party_size,
    )
    suggestion = await call(
        "suggest_reservation_need", stop_id=str(planned.stops[0].id)
    )
    assert suggestion.suggested == need


# --- dates and stops -------------------------------------------------------


async def test_dates_and_stops_through_the_tools(
    db: Factory, seeded: dict[str, Any]
) -> None:
    planned = await new_date(seeded, max_leg_minutes=40, start_after="08:00:00")
    assert planned.status == "planning"
    assert planned.max_leg_minutes == 40
    planned = await add(planned.id, title="Hike", kind="activity")
    planned = await add(planned.id, title="Dinner", kind="meal")
    planned = await add(planned.id, title="Coffee", kind="activity", position=0)
    assert [s.title for s in planned.stops] == ["Coffee", "Hike", "Dinner"]

    ids = [s.id for s in planned.stops]
    planned = await call(
        "reorder_stops", date_id=str(planned.id), stop_ids=[ids[1], ids[0], ids[2]]
    )
    assert [s.title for s in planned.stops] == ["Hike", "Coffee", "Dinner"]

    planned = await call("remove_stop", stop_id=ids[0])
    assert [s.title for s in planned.stops] == ["Hike", "Dinner"]

    renamed = await call(
        "update_date", date_id=str(planned.id), patch={"title": "Coast day"}
    )
    assert renamed.title == "Coast day"
    fetched = await call("get_date", date_id=str(planned.id))
    assert [s.title for s in fetched.stops] == ["Hike", "Dinner"]

    listed = await call("list_dates", couple_id=str(seeded["couple_id"]), day_from=DAY)
    assert [d.id for d in listed] == [planned.id]


async def test_status_can_only_be_set_to_completed_or_cancelled(
    db: Factory, seeded: dict[str, Any]
) -> None:
    planned = await new_date(seeded)
    with pytest.raises(ToolError):
        await call(
            "update_date", date_id=str(planned.id), patch={"status": "confirmed"}
        )
    with pytest.raises(ToolError, match="InvalidTransition"):
        await call(
            "update_date",
            date_id=str(seeded["loved_date_id"]),
            patch={"status": "cancelled"},
        )


async def test_list_dates_can_be_windowed(db: Factory, seeded: dict[str, Any]) -> None:
    today = datetime.now(LA).date()
    recent = await call(
        "list_dates",
        couple_id=str(seeded["couple_id"]),
        day_from=(today - timedelta(days=20)).isoformat(),
    )
    assert [d.id for d in recent] == [str(seeded["meh_date_id"])]


# --- planning --------------------------------------------------------------


async def test_get_sunset_by_venue_and_by_point(
    db: Factory, seeded: dict[str, Any]
) -> None:
    by_venue = await call("get_sunset", day=DAY, venue_id=str(seeded["nobu_id"]))
    by_point = await call("get_sunset", day=DAY, lat=34.0389596, lng=-118.6696041)
    assert by_venue.sunset == by_point.sunset
    assert by_venue.sunset.hour == 18 and by_venue.sunset.minute in (9, 10)
    with pytest.raises(ToolError, match="MissingCoordinates"):
        await call("get_sunset", day=DAY, venue_id=str(seeded["bestia_id"]))


async def test_estimate_travel_and_its_cache(
    db: Factory, seeded: dict[str, Any]
) -> None:
    first = await call(
        "estimate_travel",
        from_venue_id=str(seeded["getty_id"]),
        to_venue_id=str(seeded["nobu_id"]),
    )
    assert (first.minutes, first.provider, first.is_estimate) == (25, "haversine", True)
    assert first.from_cache is False
    again = await call(
        "estimate_travel",
        from_venue_id=str(seeded["getty_id"]),
        to_venue_id=str(seeded["nobu_id"]),
    )
    assert again.from_cache is True
    with pytest.raises(ToolError, match="MissingCoordinates"):
        await call(
            "estimate_travel",
            from_venue_id=str(seeded["getty_id"]),
            to_venue_id=str(seeded["bestia_id"]),
        )


async def test_the_acceptance_day(db: Factory, seeded: dict[str, Any]) -> None:
    """Spec section 6: the user's own plan, partly booked, gaps filled in.

    1. A short hike at Solstice Canyon.
    2. The Getty Villa, already reserved — entry at 13:00.
    3. A sunset dinner, venue and reservation to be decided.
    """
    # Haversine runs slow along PCH (Solstice -> Getty comes out at 43 min),
    # so "nearby" for a coast day is 45.
    planned = await new_date(seeded, max_leg_minutes=45)
    date_id = str(planned.id)
    planned = await add(
        date_id,
        title="Solstice Canyon hike",
        kind="activity",
        venue_id=str(seeded["solstice_id"]),
        est_duration_min=105,
        reservation_need="none",
    )
    planned = await add(
        date_id,
        title="Getty Villa",
        kind="ticketed",
        venue_id=str(seeded["getty_id"]),
        anchor="fixed",
        start_at="2026-10-24T13:00:00",
        est_duration_min=120,
        reservation_need="required",
        booking_status="confirmed",
        confirmation_number="GV-123",
    )
    planned = await add(
        date_id,
        title="Sunset dinner",
        kind="meal",
        anchor="sunset",
        est_duration_min=90,
    )
    hike, getty, dinner = planned.stops
    assert hike.booking_status == "not_needed"
    assert getty.booking_status == "confirmed"
    assert dinner.venue_id is None
    # The dinner still has to be chosen and booked.
    assert planned.status == "booking_pending"

    # Choosing the dinner: within "nearby" of the Getty Villa.
    leg = await call(
        "estimate_travel",
        from_venue_id=str(seeded["getty_id"]),
        to_venue_id=str(seeded["nobu_id"]),
    )
    assert leg.minutes <= planned.max_leg_minutes
    planned = await call(
        "update_stop", stop_id=dinner.id, patch={"venue_id": str(seeded["nobu_id"])}
    )
    need = await call("suggest_reservation_need", stop_id=dinner.id)
    assert need.suggested == "recommended"  # Nobu takes phone bookings
    planned = await call(
        "update_stop",
        stop_id=dinner.id,
        # Recommended + sunset-anchored: the skill books it.
        patch={"venue_id": str(seeded["nobu_id"]), "reservation_need": "recommended"},
    )

    preview = await call("plan_timeline", date_id=date_id)
    assert preview.applied is False
    assert preview.violations == []
    times = {
        s.title: (s.start_at, s.end_at, s.travel_min_from_prev) for s in preview.stops
    }
    hike_start, hike_end, _ = times["Solstice Canyon hike"]
    getty_start, _, to_getty = times["Getty Villa"]
    dinner_start, _, _ = times["Sunset dinner"]
    # hike end + travel + buffer <= the Getty entry time.
    assert hike_end + timedelta(minutes=to_getty + 10) <= getty_start
    assert getty_start == datetime(2026, 10, 24, 13, 0, tzinfo=LA)
    # Sunset at Nobu is 18:10; -30 rounds to 17:45.
    assert dinner_start == datetime(2026, 10, 24, 17, 45, tzinfo=LA)
    assert preview.sunset.hour == 18
    assert [leg.minutes for leg in preview.legs] == [to_getty, leg.minutes]

    # apply=False wrote nothing.
    unchanged = await call("get_date", date_id=date_id)
    assert [s.start_at for s in unchanged.stops] == [
        None,
        getty.start_at,
        None,
    ]

    applied = await call("plan_timeline", date_id=date_id, apply=True)
    assert applied.applied is True
    stored = await call("get_date", date_id=date_id)
    assert [s.travel_min_from_prev for s in stored.stops] == [
        None,
        to_getty,
        leg.minutes,
    ]
    assert stored.status == "booking_pending"

    # Dinner is phone-only, one calendar month ahead from 10:00: a month out
    # the lines aren't open yet, so a reminder goes in ...
    window = await call(
        "compute_booking_window", stop_id=dinner.id, now="2026-09-20T09:00:00"
    )
    assert window.state == "not_yet_open"
    assert window.opens_at == datetime(2026, 9, 24, 10, 0, tzinfo=LA)
    stored = await call(
        "update_stop",
        stop_id=dinner.id,
        patch={"booking_status": "reminder_set", "reminder_event_id": "evt_call"},
    )
    assert stored.status == "booking_pending"
    # ... and once it's booked, the whole date is confirmed.
    stored = await call(
        "update_stop",
        stop_id=dinner.id,
        patch={"booking_status": "confirmed", "confirmation_number": "NOBU-9"},
    )
    assert stored.status == "confirmed"


async def test_timeline_reports_missing_coordinates_and_reorders_nothing(
    db: Factory, seeded: dict[str, Any]
) -> None:
    planned = await new_date(seeded)
    planned = await add(
        planned.id,
        title="Pasta",
        kind="meal",
        venue_id=str(seeded["bestia_id"]),
        est_duration_min=90,
    )
    planned = await add(
        planned.id,
        title="Hike",
        kind="activity",
        venue_id=str(seeded["solstice_id"]),
        est_duration_min=60,
    )
    result = await call("plan_timeline", date_id=str(planned.id))
    assert [s.title for s in result.stops] == ["Pasta", "Hike"]
    assert [(v.type, v.stop_id) for v in result.violations] == [
        ("missing_coordinates", planned.stops[0].id)
    ]
    assert result.legs[0].minutes is None


async def test_a_fixed_stop_without_a_time_is_an_error(
    db: Factory, seeded: dict[str, Any]
) -> None:
    planned = await new_date(seeded)
    planned = await add(planned.id, title="Getty", kind="ticketed", anchor="fixed")
    with pytest.raises(ToolError, match="IncompleteStop"):
        await call("plan_timeline", date_id=str(planned.id))


async def test_applying_keeps_a_booked_stops_end_without_a_duration(
    db: Factory, seeded: dict[str, Any]
) -> None:
    planned = await new_date(seeded)
    planned = await add(
        planned.id,
        title="Dinner",
        kind="meal",
        venue_id=str(seeded["nobu_id"]),
        anchor="fixed",
        reservation_need="required",
        booking_status="confirmed",
        start_at="2026-10-24T19:00:00-07:00",
        end_at="2026-10-24T20:30:00-07:00",
    )
    assert planned.status == "confirmed"
    result = await call("plan_timeline", date_id=str(planned.id), apply=True)
    assert result.violations == []
    stored = await call("get_date", date_id=str(planned.id))
    assert stored.stops[0].end_at == datetime.fromisoformat("2026-10-24T20:30:00-07:00")
    assert stored.status == "confirmed"
