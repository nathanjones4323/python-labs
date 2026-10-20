"""The tools as a client sees them, over an in-memory MCP connection.

These go through FastMCP's own plumbing — schema validation on the way in,
serialisation on the way out — so they catch what a repo-level test can't:
a tool that takes an argument an agent can't express, or hands back something
that won't serialise.
"""

import contextlib
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import jsonschema
import pytest
import respx
from date_planner.db import repo
from date_planner.planning import forecast
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


async def profile_of(couple_id: Any) -> Any:
    """The couple's profile, as `list_couples` hands it back."""
    return next(c for c in await call("list_couples") if str(c.id) == str(couple_id))


async def add(date_id: Any, position: int | None = None, **stop: Any) -> Any:
    return await call("add_stop", date_id=str(date_id), stop=stop, position=position)


async def test_ping_needs_no_database() -> None:
    assert await call("ping") == "pong"


async def test_every_tool_has_a_description() -> None:
    async with client() as connected:
        tools = await connected.list_tools()
    assert len(tools) == 29
    assert all(tool.description for tool in tools)


# --- couples ---------------------------------------------------------------


async def test_profile_round_trip(db: Factory, seeded: dict[str, Any]) -> None:
    profile = await profile_of(seeded["couple_id"])
    assert profile.name == "Test couple"
    assert profile.budget_per_date == 250
    assert [m.name for m in profile.members] == ["Alex", "Sam"]
    assert profile.members[0].dietary == ["no shellfish"]
    # Sam's dislike and Alex's dietary need bind a date for both.
    assert profile.shared.likes == ["sushi", "hiking"]
    assert profile.shared.dislikes == ["loud rooms"]
    assert profile.shared.dietary == ["no shellfish"]

    updated = await call(
        "upsert_couple_profile",
        profile={"id": str(seeded["couple_id"]), "budget_per_date": 180},
    )
    assert updated.budget_per_date == 180
    assert [m.name for m in updated.members] == ["Alex", "Sam"]


async def test_a_new_profile_can_be_created_from_nothing(db: Factory) -> None:
    created = await call("upsert_couple_profile", profile={"name": "New couple"})
    assert created.members == []
    assert created.next_turn is None
    listed = await call("list_couples")
    assert [c.id for c in listed] == [created.id]

    with_member = await call(
        "upsert_member", couple_id=str(created.id), member={"name": "Cy"}
    )
    assert [m.name for m in with_member.members] == ["Cy"]
    assert with_member.next_turn.name == "Cy"


async def test_preferences_are_edited_per_member(
    db: Factory, seeded: dict[str, Any]
) -> None:
    edited = await call(
        "edit_member_preferences",
        couple_id=str(seeded["couple_id"]),
        member="sam",
        edit={"add_likes": ["tapas"], "add_dislikes": ["sushi"]},
    )
    alex, sam = edited.members
    assert sam.likes == ["hiking", "tapas"]
    assert sam.dislikes == ["loud rooms", "sushi"]
    # Alex is untouched; sushi is now vetoed for a date for both.
    assert alex.likes == ["sushi"]
    assert edited.shared.likes == ["hiking", "tapas"]


async def test_editing_an_unknown_member_is_a_not_found_error(
    db: Factory, seeded: dict[str, Any]
) -> None:
    with pytest.raises(ToolError, match="NotFound.*members: Alex, Sam"):
        await call(
            "edit_member_preferences",
            couple_id=str(seeded["couple_id"]),
            member="Cy",
            edit={"add_likes": ["tapas"]},
        )


async def test_planning_for_a_member_passes_the_turn_on(
    db: Factory, seeded: dict[str, Any]
) -> None:
    couple_id = str(seeded["couple_id"])
    before = await profile_of(couple_id)
    assert before.next_turn.name == "Alex"

    with pytest.raises(ToolError, match="NotFound"):
        await new_date(seeded, planned_for="nobody")

    both = await new_date(seeded)
    assert both.planned_for is None
    assert (await profile_of(couple_id)).next_turn.name == "Alex"

    hers = await new_date(seeded, planned_for="a@example.com")
    assert hers.planned_for == "Alex"
    after = await profile_of(couple_id)
    assert after.next_turn.name == "Sam"
    assert after.next_turn.last_planned_for is None  # Sam hasn't had one yet


async def test_past_dates_come_back_with_ratings_and_stops(
    db: Factory, seeded: dict[str, Any]
) -> None:
    past = await call("list_past_dates", couple_id=str(seeded["couple_id"]))
    assert [p.title for p in past] == ["Pasta at Bestia", "Sushi at Nobu"]
    assert [[r.score for r in p.ratings] for p in past] == [[6], [10]]
    assert [p.stops[0].venue_name for p in past] == ["Bestia", "Nobu Malibu"]


async def test_rating_a_date_through_the_tools(
    db: Factory, seeded: dict[str, Any]
) -> None:
    loved = str(seeded["loved_date_id"])
    stop_id = str((await call("get_date", date_id=loved)).stops[0].id)
    rated = await call(
        "rate_date",
        date_id=loved,
        member="Sam",
        score=3,
        notes="Too loud",
        stops=[{"stop_id": stop_id, "score": 2, "notes": "Couldn't talk"}],
        source="preview",
    )
    by_member = {r.member: r for r in rated.ratings}
    assert (by_member["Alex"].score, by_member["Sam"].score) == (10, 3)
    assert by_member["Sam"].notes == "Too loud"
    assert by_member["Sam"].source == "preview"
    [stop_rating] = rated.stops[0].ratings
    assert (stop_rating.member, stop_rating.score) == ("Sam", 2)

    with pytest.raises(ToolError):
        await call("rate_date", date_id=loved, member="Sam", score=11)
    with pytest.raises(ToolError, match="IncompleteRating"):
        await call("rate_date", date_id=loved, member="Sam")
    with pytest.raises(ToolError):
        await call("update_date", date_id=loved, patch={"rating": 5})

    cleared = await call("clear_rating", date_id=loved, member="Sam", stop_id=stop_id)
    assert cleared.stops[0].ratings == []


async def test_an_unknown_couple_is_a_not_found_error(db: Factory) -> None:
    with pytest.raises(ToolError, match="NotFound"):
        await call(
            "upsert_member",
            couple_id="00000000-0000-0000-0000-000000000000",
            member={"name": "Cy"},
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


# --- the Resy connector ----------------------------------------------------

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
BESTIA_PLACE = {
    "id": "ChIJ-bestia",
    "displayName": {"text": "Bestia"},
    "formattedAddress": "2121 E 7th Pl, Los Angeles, CA 90021, USA",
    "location": {"latitude": 34.0337, "longitude": -118.2294},
}


async def test_find_google_place_saves_the_match_once(
    db: Factory, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k3y")
    namesake = {"id": "ChIJ-bavel", "displayName": {"text": "Bavel"}}
    with respx.mock() as router:
        route = router.post(PLACES_URL).respond(
            200, json={"places": [namesake, BESTIA_PLACE]}
        )
        found = await call("find_google_place", venue_id=str(seeded["bestia_id"]))
        assert found.google_place_id == "ChIJ-bestia"
        assert [c.is_match for c in found.candidates] == [False, True]
        # Bestia had no coordinates or address on file.
        assert found.filled == ["lat", "lng", "address"]

        again = await call("find_google_place", venue_id=str(seeded["bestia_id"]))
        assert again.from_cache is True
        assert again.google_place_id == "ChIJ-bestia"
        assert route.call_count == 1

    venue = await call("find_venue", name="Bestia", city="Los Angeles")
    assert venue.google_place_id == "ChIJ-bestia"
    assert venue.lat == 34.0337
    async with db() as session:
        row = await repo.get_venue(session, seeded["bestia_id"])
        assert row.google_place_data == BESTIA_PLACE


async def test_choosing_another_place_drops_the_saved_response(
    db: Factory, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k3y")
    with respx.mock() as router:
        router.post(PLACES_URL).respond(200, json={"places": [BESTIA_PLACE]})
        await call("find_google_place", venue_id=str(seeded["bestia_id"]))

    # Same id again: the response still describes it.
    await call(
        "upsert_venue",
        venue={"id": str(seeded["bestia_id"]), "google_place_id": "ChIJ-bestia"},
    )
    async with db() as session:
        row = await repo.get_venue(session, seeded["bestia_id"])
        assert row.google_place_data == BESTIA_PLACE

    await call(
        "upsert_venue",
        venue={"id": str(seeded["bestia_id"]), "google_place_id": "ChIJ-other"},
    )
    async with db() as session:
        row = await repo.get_venue(session, seeded["bestia_id"])
        assert row.google_place_id == "ChIJ-other"
        assert row.google_place_data is None


async def test_a_reworded_venue_is_found_and_says_so(
    db: Factory, seeded: dict[str, Any]
) -> None:
    found = await call("find_venue", name="Nobu", city="Malibu")
    assert found.name == "Nobu Malibu"
    assert found.matched_name == "Nobu"
    exact = await call("find_venue", name="nobu malibu", city="Malibu")
    assert exact.matched_name is None

    again = await call(
        "upsert_venue", venue={"name": "Nobu Malibu Restaurant", "city": "Malibu"}
    )
    assert again.id == found.id
    assert again.name == "Nobu Malibu"
    assert again.matched_name == "Nobu Malibu Restaurant"


async def test_find_google_place_saves_nothing_without_a_match(
    db: Factory, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k3y")
    with respx.mock() as router:
        router.post(PLACES_URL).respond(
            200, json={"places": [{"id": "x", "displayName": {"text": "Bavel"}}]}
        )
        found = await call("find_google_place", venue_id=str(seeded["bestia_id"]))
    assert found.google_place_id is None
    assert found.filled == []
    venue = await call("find_venue", name="Bestia", city="Los Angeles")
    assert venue.google_place_id is None


async def test_find_google_place_needs_a_key(
    db: Factory, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "")
    with pytest.raises(ToolError, match="PlacesFailed: GOOGLE_PLACES_API_KEY"):
        await call("find_google_place", venue_id=str(seeded["bestia_id"]))


async def bestia_stop(seeded: dict[str, Any], **stop: Any) -> str:
    planned = await new_date(seeded)
    planned = await add(
        planned.id,
        title="Dinner at Bestia",
        kind="meal",
        venue_id=str(seeded["bestia_id"]),
        start_at="2026-10-24T19:00:00",
        **stop,
    )
    return str(planned.stops[0].id)


async def test_resy_widget_args(db: Factory, seeded: dict[str, Any]) -> None:
    stop_id = await bestia_stop(seeded, party_size=4)
    with pytest.raises(ToolError, match="MissingGooglePlace"):
        await call("resy_widget_args", stop_id=stop_id, now="2026-10-11T09:00:00")

    await call(
        "upsert_venue",
        venue={"id": str(seeded["bestia_id"]), "google_place_id": "ChIJ-bestia"},
    )
    args = await call("resy_widget_args", stop_id=stop_id, now="2026-10-11T09:00:00")
    assert args.google_places_id == "ChIJ-bestia"
    assert args.restaurant_name == "Bestia"
    assert args.date == "2026-10-24"
    assert args.party_size == 4
    assert args.resy_url == (
        "https://resy.com/cities/los-angeles-ca/venues/bestia?date=2026-10-24&seats=4"
    )
    assert args.local_start == datetime(2026, 10, 24, 19, 0, tzinfo=LA)


@pytest.mark.parametrize(
    ("now", "party_size", "reason"),
    [
        ("2026-08-24T09:00:00", 2, "comes into range on 2026-08-25"),
        ("2026-10-25T09:00:00", 2, "has passed"),
        ("2026-10-11T09:00:00", 16, "1–15 people, not 16"),
    ],
)
async def test_resy_widget_range(
    db: Factory, seeded: dict[str, Any], now: str, party_size: int, reason: str
) -> None:
    await call(
        "upsert_venue",
        venue={"id": str(seeded["bestia_id"]), "google_place_id": "ChIJ-bestia"},
    )
    stop_id = await bestia_stop(seeded, party_size=party_size)
    with pytest.raises(ToolError, match=f"OutsideWidgetRange: .*{reason}"):
        await call("resy_widget_args", stop_id=stop_id, now=now)


async def test_resy_widget_is_for_resy_venues(
    db: Factory, seeded: dict[str, Any]
) -> None:
    planned = await new_date(seeded)
    planned = await add(
        planned.id,
        title="Dinner at Nobu",
        kind="meal",
        venue_id=str(seeded["nobu_id"]),
        start_at="2026-10-24T19:00:00",
    )
    with pytest.raises(ToolError, match="UnsupportedChannel: .*phone, not Resy"):
        await call("resy_widget_args", stop_id=str(planned.stops[0].id))


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


async def test_local_times_pass_a_strict_output_schema(
    db: Factory, seeded: dict[str, Any]
) -> None:
    planned = await new_date(seeded, start_after="09:00", end_before="22:00")
    async with client() as connected:
        tools = {t.name: t for t in await connected.list_tools()}
        result = await connected.call_tool("get_date", {"date_id": str(planned.id)})
    schema = tools["get_date"].output_schema
    assert schema is not None
    # FormatChecker only checks `time` with rfc3339-validator installed, so
    # look for the format itself too.
    assert '"format": "time"' not in json.dumps(schema)
    assert result.structured_content is not None
    jsonschema.validate(
        result.structured_content, schema, format_checker=jsonschema.FormatChecker()
    )


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


async def test_get_forecast_for_a_stop_at_a_venue(
    db: Factory, seeded: dict[str, Any]
) -> None:
    day = datetime.now(LA).date() + timedelta(days=3)
    with respx.mock() as router:
        route = router.get(forecast.FORECAST_URL).respond(
            200,
            json={
                "hourly": {
                    "time": [f"{day}T{h:02d}:00" for h in range(24)],
                    **{name: [0] * 24 for name in forecast.HOURLY},
                }
            },
        )
        result = await call(
            "get_forecast",
            day=day.isoformat(),
            venue_id=str(seeded["solstice_id"]),
            from_time="09:00",
            to_time="11:00",
        )
    assert route.calls.last.request.url.params["latitude"] == "34.03765"
    assert result.available and result.timezone == "America/Los_Angeles"
    assert [h.time.hour for h in result.hours] == [9, 10]
    assert result.hours[0].conditions == "clear"

    far = await call(
        "get_forecast",
        day=(day + timedelta(days=30)).isoformat(),
        lat=34.0,
        lng=-118.5,
    )
    assert not far.available and "check again from" in far.reason
    with pytest.raises(ToolError, match="MissingCoordinates"):
        await call(
            "get_forecast", day=day.isoformat(), venue_id=str(seeded["bestia_id"])
        )


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


async def test_timeline_flags_calendar_conflicts_but_not_its_own_holds(
    db: Factory, seeded: dict[str, Any]
) -> None:
    planned = await new_date(seeded)
    planned = await add(
        planned.id,
        title="Getty Villa",
        kind="ticketed",
        venue_id=str(seeded["getty_id"]),
        anchor="fixed",
        start_at="2026-10-24T13:00:00-07:00",
        est_duration_min=120,
    )
    getty = planned.stops[0].id
    await call("update_stop", stop_id=getty, patch={"calendar_event_id": "hold-1"})
    busy = [
        {
            "title": "[TENTATIVE] Getty Villa",
            "start": "2026-10-24T13:00:00-07:00",
            "end": "2026-10-24T15:00:00-07:00",
            "event_id": "hold-1",
        },
        {
            "title": "Birthday lunch",
            "start": "2026-10-24T12:00:00-07:00",
            "end": "2026-10-24T13:30:00-07:00",
        },
    ]
    result = await call("plan_timeline", date_id=str(planned.id), busy=busy)
    assert [(v.type, v.stop_id) for v in result.violations] == [
        ("calendar_conflict", getty)
    ]
    assert '"Birthday lunch", 12:00-13:30' in result.violations[0].message
