"""Tools for fitting the day together: sunset, travel, and the timeline.

`plan_timeline` checks and places; the agent decides the order. It's
deterministic, so running it again after a change is cheap — the point is to
iterate until there are no violations, then `apply=True`.
"""

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastmcp import FastMCP

from date_planner.config import get_settings
from date_planner.db import open_session, repo
from date_planner.db.models import (
    DEFAULT_TIMEZONE,
    Anchor,
    TravelMode,
    Venue,
    VenueType,
)
from date_planner.errors import MissingCoordinates
from date_planner.planning import sunset, timeline, travel
from date_planner.schemas import (
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
        if venue_id is not None:
            async with open_session() as session:
                venue = await repo.get_venue(session, venue_id)
                if venue.lat is None or venue.lng is None:
                    raise MissingCoordinates(
                        f"{venue.name} has no lat/lng; save them with upsert_venue."
                    )
                lat, lng, tz = venue.lat, venue.lng, venue.tz
        else:
            if lat is None or lng is None:
                raise MissingCoordinates("Pass a venue_id, or both lat and lng")
            tz = ZoneInfo(timezone or DEFAULT_TIMEZONE)
        result = sunset.compute(lat, lng, day, tz)
        return SunsetOut(
            sunset=result.sunset,
            civil_dusk=result.civil_dusk,
            golden_hour_start=result.golden_hour_start,
        )

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
            return TravelOut(
                minutes=leg.minutes,
                meters=leg.meters,
                provider=leg.provider,
                is_estimate=leg.is_estimate,
                from_cache=leg.from_cache,
            )

    @mcp.tool
    @as_tool_error
    async def plan_timeline(
        date_id: uuid.UUID, buffer_min: int = 10, apply: bool = False
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
        `missing_coordinates`, `daylight` (a trail ending after sunset).

        Nothing is written unless `apply=True`; do that once the user has
        approved the timeline.
        """
        settings = get_settings()
        async with open_session() as session:
            planned = await repo.get_date(session, date_id)
            tz = (await repo.get_couple(session, planned.couple_id)).tz
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
            leg_minutes: list[int | None] = []
            for previous, stop in zip(stops, stops[1:]):
                origin, dest = previous.venue, stop.venue
                found = None
                if origin is not None and dest is not None:
                    if origin.has_coordinates and dest.has_coordinates:
                        found = await travel.estimate(
                            session, origin, dest, stop.travel_mode, settings
                        )
                leg_minutes.append(found.minutes if found else None)
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
                inputs.append(
                    timeline.StopInput(
                        id=stop.id,
                        title=stop.title,
                        anchor=stop.anchor,
                        duration_min=stop.est_duration_min,
                        start_at=stop.start_at,
                        sunset_offset_min=stop.sunset_offset_min,
                        sunset=stop_sunset,
                        is_trail=(
                            stop.venue is not None
                            and stop.venue.venue_type is VenueType.TRAIL
                        ),
                    )
                )

            result = timeline.plan(
                inputs,
                leg_minutes,
                day=planned.day,
                tz=tz,
                buffer_min=buffer_min,
                max_leg_minutes=planned.max_leg_minutes,
                start_after=planned.start_after,
                end_before=planned.end_before,
            )
            violations += [
                ViolationOut(type=v.type, message=v.message, stop_id=v.stop_id)
                for v in result.violations
            ]

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
