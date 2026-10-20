"""Tools for fitting the day together: sunset, weather, travel, and the timeline.

`plan_timeline` checks and places; the agent decides the order. It's
deterministic, so running it again after a change is cheap — the point is to
iterate until there are no violations, then `apply=True`.
"""

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastmcp import FastMCP
from sqlalchemy.ext.asyncio import AsyncSession

from date_planner.config import get_settings
from date_planner.db import open_session, repo
from date_planner.db.models import (
    DEFAULT_TIMEZONE,
    Anchor,
    TravelMode,
    Venue,
    VenueType,
)
from date_planner.errors import MissingCoordinates, UnknownTimezone
from date_planner.planning import forecast, sunset, timeline, travel
from date_planner.schemas import (
    BusyEventIn,
    ForecastOut,
    ForecastPlaceIn,
    LegOut,
    PlacedStopOut,
    SunsetOut,
    TimelineOut,
    TravelOut,
    ViolationOut,
)
from date_planner.tools import as_tool_error


def _sunset_at(venue: Venue | None, day: date, tz: ZoneInfo) -> datetime | None:
    if venue is None or venue.lat is None or venue.lng is None:
        return None
    return sunset.compute(venue.lat, venue.lng, day, tz).sunset


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UnknownTimezone(
            f"{name!r} is not a timezone; use an IANA name like America/Los_Angeles"
        ) from exc


async def _place(
    session: AsyncSession,
    venue_id: uuid.UUID | None,
    lat: float | None,
    lng: float | None,
    timezone: str | None,
) -> tuple[float, float, ZoneInfo]:
    """A venue's coordinates and timezone, or the given point's."""
    if venue_id is not None:
        venue = await repo.get_venue(session, venue_id)
        point = travel.coordinates(venue)
        return point.lat, point.lng, _zone(venue.timezone)
    if lat is None or lng is None:
        raise MissingCoordinates("Pass a venue_id, or both lat and lng")
    return lat, lng, _zone(timezone or DEFAULT_TIMEZONE)


def register(mcp: FastMCP) -> None:
    @mcp.tool
    @as_tool_error
    async def get_sunset(
        day: date,
        venue_id: uuid.UUID | None = None,
        lat: float | None = None,
        lng: float | None = None,
        timezone: str | None = None,
    ) -> SunsetOut:
        """Sunset, civil dusk and the start of golden hour on `day`.

        Pass a `venue_id`, or `lat` + `lng`. Times are in the venue's
        timezone (or `timezone`, default America/Los_Angeles). Computed
        locally — no network. Civil dusk is the end of usable light on a
        trail.
        """
        async with open_session() as session:
            lat, lng, tz = await _place(session, venue_id, lat, lng, timezone)
        return SunsetOut.model_validate(sunset.compute(lat, lng, day, tz))

    @mcp.tool
    @as_tool_error
    async def get_forecast(
        day: date, places: list[ForecastPlaceIn]
    ) -> list[ForecastOut]:
        """The hourly weather forecast for `day` at each of `places`: rain,
        heat, wind, and low cloud that can hide a sunset.

        Pass every stop you want weather for in one call — one request
        covers them all. Each place is a `venue_id`, or `lat` + `lng`, with
        an optional `from_time` / `to_time` (local, HH:MM) to keep only the
        hours overlapping that stop; otherwise the whole day. Results come
        back in the same order, in each venue's timezone (or `timezone`,
        default America/Los_Angeles). °F, mph, inches.

        Covers today to 15 days ahead (Open-Meteo's 16-day forecast). Outside
        that, nothing is fetched: `available` is false and `reason` says
        when to check again. Live data, fetched on every call and never
        saved; it's a forecast, not a promise.
        """
        wanted = []
        async with open_session() as session:
            for place in places:
                lat, lng, tz = await _place(
                    session, place.venue_id, place.lat, place.lng, place.timezone
                )
                wanted.append(
                    forecast.Place(lat, lng, tz, place.from_time, place.to_time)
                )
        results = await forecast.fetch(day, wanted)
        return [
            ForecastOut.model_validate(result).model_copy(
                update={"venue_id": place.venue_id}
            )
            for place, result in zip(places, results)
        ]

    @mcp.tool
    @as_tool_error
    async def estimate_travel(
        from_venue_id: uuid.UUID,
        to_venue_id: uuid.UUID,
        mode: TravelMode = TravelMode.DRIVING,
    ) -> TravelOut:
        """How long it takes to get from one venue to another.

        Use it to keep candidates within the date's `max_leg_minutes` of
        their neighbouring stops. Driving times include a traffic padding
        (20% by default). `is_estimate` true means a straight-line estimate,
        not a routed one — on the coast it runs on the slow side. Raises
        `MissingCoordinates` if either venue has no lat/lng: look them up
        and save them with `upsert_venue`, then ask again.
        """
        async with open_session() as session:
            origin = await repo.get_venue(session, from_venue_id)
            dest = await repo.get_venue(session, to_venue_id)
            leg = await travel.estimate(session, origin, dest, mode, get_settings())
            return TravelOut.model_validate(leg)

    @mcp.tool
    @as_tool_error
    async def plan_timeline(
        date_id: uuid.UUID,
        buffer_min: int = 10,
        apply: bool = False,
        busy: list[BusyEventIn] | None = None,
    ) -> TimelineOut:
        """Work out every stop's time in the current order, and what's wrong.

        Fixed stops keep their time; sunset stops go at sunset +
        `sunset_offset_min`, rounded to 15 minutes; flexible stops fill in
        around them with each leg's travel time plus `buffer_min`. It never
        reorders — change the order with `reorder_stops` (or a duration with
        `update_stop`) and run it again.

        Violations: `leg_exceeds_max` (a leg over the date's
        `max_leg_minutes`), `overlap` (a stop can't fit before the next
        anchor), `outside_window`, `missing_duration`,
        `missing_coordinates`, `daylight` (a trail ending after sunset),
        `calendar_conflict` (a stop overlapping one of `busy`).

        `busy` is the day's events from the shared calendar. Events whose
        `event_id` is one of this date's own holds or call reminders are
        skipped, so a date never conflicts with itself.

        Nothing is written unless `apply=True`; do that once the user has
        approved the timeline.
        """
        settings = get_settings()
        # One HTTP client for every leg, rather than one per routing call.
        async with (
            httpx.AsyncClient(timeout=settings.fetch_timeout) as client,
            open_session() as session,
        ):
            planned = await repo.get_date(session, date_id)
            tz = await repo.date_tz(session, planned)
            stops = list(planned.stops)
            violations: list[ViolationOut] = []

            sunsets = [_sunset_at(s.venue, planned.day, tz) for s in stops]
            # Where a stop's own venue isn't known yet, the day's sunset
            # elsewhere on the route is close enough to anchor on.
            day_sunset = next((s for s in sunsets if s is not None), None)

            flagged: set[uuid.UUID] = set()

            def missing(stop_id: uuid.UUID, message: str) -> None:
                if stop_id not in flagged:
                    flagged.add(stop_id)
                    violations.append(
                        ViolationOut(
                            type=timeline.ViolationType.MISSING_COORDINATES,
                            message=message,
                            stop_id=stop_id,
                        )
                    )

            for stop in stops:
                if stop.venue is None:
                    missing(stop.id, f'"{stop.title}" has no venue yet.')
                elif not stop.venue.has_coordinates:
                    missing(
                        stop.id,
                        f'{stop.venue.name} ("{stop.title}") has no lat/lng; '
                        "save them with upsert_venue.",
                    )

            legs: list[LegOut] = []
            for previous, stop in zip(stops, stops[1:]):
                origin, dest = previous.venue, stop.venue
                found = None
                if (
                    origin is not None
                    and dest is not None
                    and origin.has_coordinates
                    and dest.has_coordinates
                ):
                    found = await travel.estimate(
                        session, origin, dest, stop.travel_mode, settings, client=client
                    )
                legs.append(
                    LegOut(
                        from_stop_id=previous.id,
                        to_stop_id=stop.id,
                        minutes=found.minutes if found else None,
                        meters=found.meters if found else None,
                        provider=found.provider if found else None,
                        is_estimate=found.is_estimate if found else None,
                        mode=stop.travel_mode,
                    )
                )

            inputs = []
            for stop, own_sunset in zip(stops, sunsets):
                stop_sunset = own_sunset or day_sunset
                if stop.anchor is Anchor.SUNSET and stop_sunset is None:
                    missing(
                        stop.id,
                        f'"{stop.title}" is anchored on sunset, but no stop has '
                        "coordinates to work sunset out from.",
                    )
                # A stop with no estimate but both times set (a booked dinner,
                # 19:00-20:30) lasts as long as those say, so applying the
                # timeline doesn't wipe its end_at.
                duration = stop.est_duration_min
                if (
                    duration is None
                    and stop.start_at is not None
                    and stop.end_at is not None
                ):
                    duration = int((stop.end_at - stop.start_at).total_seconds() // 60)
                inputs.append(
                    timeline.StopInput(
                        id=stop.id,
                        title=stop.title,
                        anchor=stop.anchor,
                        duration_min=duration,
                        start_at=stop.start_at,
                        sunset_offset_min=stop.sunset_offset_min,
                        sunset=stop_sunset,
                        is_trail=(
                            stop.venue is not None
                            and stop.venue.venue_type is VenueType.TRAIL
                        ),
                    )
                )

            own_events = {
                event_id
                for stop in stops
                for event_id in (stop.calendar_event_id, stop.reminder_event_id)
                if event_id
            }
            blocks = [
                timeline.Busy(title=event.title, start=event.start, end=event.end)
                for event in busy or []
                if event.event_id is None or event.event_id not in own_events
            ]

            result = timeline.plan(
                inputs,
                [leg.minutes for leg in legs],
                day=planned.day,
                tz=tz,
                buffer_min=buffer_min,
                max_leg_minutes=planned.max_leg_minutes,
                start_after=planned.start_after,
                end_before=planned.end_before,
                busy=blocks,
            )
            violations += [ViolationOut.model_validate(v) for v in result.violations]

            if apply:
                await repo.write_timeline(
                    session,
                    planned,
                    {
                        # A stop that couldn't be placed keeps what it had.
                        placed.id: (
                            (placed.start_at, placed.end_at)
                            if placed.start_at is not None
                            else (stop.start_at, stop.end_at)
                        )
                        + (placed.travel_min_from_prev,)
                        for placed, stop in zip(result.stops, stops)
                    },
                )

            return TimelineOut(
                stops=[
                    PlacedStopOut(
                        id=placed.id,
                        title=stop.title,
                        start_at=placed.start_at,
                        end_at=placed.end_at,
                        travel_min_from_prev=placed.travel_min_from_prev,
                    )
                    for placed, stop in zip(result.stops, stops)
                ],
                legs=legs,
                sunset=day_sunset,
                max_leg_minutes=planned.max_leg_minutes,
                violations=violations,
                applied=apply,
            )
