"""Tools about venues: what we know, how they book, when they book.

`detect_booking_channel` and `find_google_place` are the only tools here
that reach out to the internet, and both cache what they learn on the venue
row so the next question is free. The booking tools take a stop, not a
venue: the stop says when and for how many.
"""

import uuid
from datetime import date, datetime, timedelta, timezone

from fastmcp import FastMCP

from date_planner.booking import detect as detection_mod
from date_planner.booking import links, places, window
from date_planner.booking.channels import ONLINE_CHANNELS, BookingChannel
from date_planner.config import get_settings
from date_planner.db import open_session, repo
from date_planner.db.models import DateStop, ReservationNeed, Venue
from date_planner.errors import (
    DetectionFailed,
    IncompleteStop,
    MissingGooglePlace,
    OutsideWidgetRange,
    PlacesFailed,
    UnsupportedChannel,
)
from date_planner.planning import travel
from date_planner.schemas import (
    BookingLinkOut,
    BookingWindowOut,
    DetectionOut,
    GooglePlaceOut,
    PlaceCandidateOut,
    ReservationNeedOut,
    ResyWidgetOut,
    VenueIn,
    VenueOut,
)
from date_planner.tools import as_tool_error, localize


def _from_row(
    venue: Venue,
    *,
    from_cache: bool,
    pages_fetched: list[str] | None = None,
) -> DetectionOut:
    """The venue row's own answer about how it books."""
    return DetectionOut(
        venue_id=venue.id,
        channel=venue.booking_channel,
        platform_slug=venue.platform_slug,
        platform_city_slug=venue.platform_city_slug,
        phone=venue.phone,
        confidence=venue.channel_confidence or 0.0,
        evidence=venue.channel_evidence or "",
        window_amount=venue.window_amount,
        window_unit=venue.window_unit,
        window_open_time=venue.window_open_time,
        from_cache=from_cache,
        pages_fetched=pages_fetched or [],
    )


def _from_detection(found: detection_mod.Detection) -> DetectionOut:
    """A fresh detection that isn't recorded against any venue."""
    window = found.window
    return DetectionOut(
        channel=found.channel,
        platform_slug=found.platform_slug,
        platform_city_slug=found.platform_city_slug,
        phone=found.phone,
        confidence=found.confidence,
        evidence=found.evidence,
        window_amount=window.amount if window else None,
        window_unit=window.unit if window else None,
        window_open_time=window.open_time if window else None,
        pages_fetched=list(found.pages_fetched),
    )


def _is_fresh(venue: Venue, ttl_days: int) -> bool:
    checked = venue.channel_checked_at
    if checked is None:
        return False
    return datetime.now(timezone.utc) - checked < timedelta(days=ttl_days)


def _stop_venue(stop: DateStop) -> Venue:
    if stop.venue is None:
        raise IncompleteStop(
            f'"{stop.title}" has no venue yet; choose one and set it with update_stop.'
        )
    return stop.venue


def _stop_start(stop: DateStop) -> datetime:
    if stop.start_at is None:
        raise IncompleteStop(
            f'"{stop.title}" has no start_at yet; run plan_timeline(apply=True) '
            "or set the booked time with update_stop."
        )
    return stop.start_at


# The Resy connector's own limits for its availability widget.
RESY_WIDGET_DAYS_AHEAD = 60
RESY_WIDGET_MAX_PARTY = 15


def check_resy_widget_range(day: date, today: date, party_size: int) -> None:
    """Raise `OutsideWidgetRange` unless the Resy widget will take this."""
    last = today + timedelta(days=RESY_WIDGET_DAYS_AHEAD)
    if day < today:
        raise OutsideWidgetRange(f"{day} has passed.")
    if day > last:
        opens = day - timedelta(days=RESY_WIDGET_DAYS_AHEAD)
        raise OutsideWidgetRange(
            f"The Resy widget shows {RESY_WIDGET_DAYS_AHEAD} days ahead at most "
            f"({day} comes into range on {opens}); hand over build_booking_link "
            "for now."
        )
    if not 1 <= party_size <= RESY_WIDGET_MAX_PARTY:
        raise OutsideWidgetRange(
            f"The Resy widget takes 1–{RESY_WIDGET_MAX_PARTY} people, not "
            f"{party_size}; hand over build_booking_link."
        )


def suggest_need(
    channel: BookingChannel, evidence: str | None, party_size: int
) -> tuple[ReservationNeed, str]:
    """The default reservation need for a party of `party_size` at a venue
    that books through `channel`."""
    if channel is BookingChannel.WALK_IN:
        return ReservationNeed.NONE, "Walk-in only: nothing to book."
    if channel is BookingChannel.TICKETED:
        return ReservationNeed.REQUIRED, "Timed entry: no ticket, no entry."
    if channel in ONLINE_CHANNELS or channel is BookingChannel.PHONE:
        minimum = detection_mod.required_from(evidence)
        if minimum is not None and party_size >= minimum:
            return (
                ReservationNeed.REQUIRED,
                f"Books by {channel}, and the site says reservations are required.",
            )
        if minimum is not None:
            return (
                ReservationNeed.RECOMMENDED,
                f"Takes reservations by {channel}; the site requires them only "
                f"for parties of {minimum}+.",
            )
        return ReservationNeed.RECOMMENDED, f"Takes reservations by {channel}."
    return ReservationNeed.UNKNOWN, "How it books isn't known; detect or ask."


def register(mcp: FastMCP) -> None:
    @mcp.tool
    @as_tool_error
    async def find_venue(name: str, city: str) -> VenueOut | None:
        """The venue we have on file for this name and city, or null.

        Capitalisation doesn't matter. A null means we've never recorded it:
        follow with `upsert_venue`.
        """
        async with open_session() as session:
            row = await repo.find_venue(session, name, city)
            return None if row is None else VenueOut.model_validate(row)

    @mcp.tool
    @as_tool_error
    async def upsert_venue(venue: VenueIn) -> VenueOut:
        """Record a venue — restaurant, trail, museum, bar, park, viewpoint —
        or update one.

        With an `id` it updates that row; without one, `name` and `city`
        identify it, so upserting "Nobu Malibu" twice updates rather than
        duplicates. A new venue needs `venue_type`. Give `lat`/`lng` (for a
        trail, the trailhead) so legs to and from it can be timed, and
        `default_duration_min` if you know how long a visit takes. Anything a
        person tells you here outranks what detection later reads off the
        website.
        """
        async with open_session() as session:
            return VenueOut.model_validate(await repo.upsert_venue(session, venue))

    @mcp.tool
    @as_tool_error
    async def detect_booking_channel(
        venue_id: uuid.UUID | None = None,
        website_url: str | None = None,
        force: bool = False,
    ) -> DetectionOut:
        """Read a venue's website and work out how it takes reservations.

        Pass `venue_id` to detect and cache against a known venue, or
        `website_url` alone to look at a site without recording anything.
        A cached answer is reused until it's 30 days old; `force` re-reads now.

        Channels: resy / opentable / tock / sevenrooms (a deep link),
        `ticketed` (timed entry on the venue's own site; `platform_slug` is
        that booking page), `phone`, `walk_in`, `unknown`. Evidence ending in
        "reservations required" means the site said so.

        `confidence` below 0.6 means the site wasn't clear — say so and ask
        the user rather than acting on it.
        """
        settings = get_settings()
        if venue_id is None:
            if not website_url:
                raise DetectionFailed("Pass a venue_id or a website_url to detect")
            return _from_detection(
                await detection_mod.detect(website_url, settings=settings)
            )

        async with open_session() as session:
            venue = await repo.get_venue(session, venue_id)
            if not force and _is_fresh(venue, settings.detection_ttl_days):
                return _from_row(venue, from_cache=True)
            site = website_url or venue.website
            if not site:
                raise DetectionFailed(
                    f"{venue.name} has no website on file; add one with "
                    "upsert_venue, or set booking_channel by hand."
                )
            found = await detection_mod.detect(site, settings=settings)
            await repo.record_detection(session, venue, found)
            # Report the row rather than the raw detection:
            # `record_detection` keeps a phone or window a person typed in.
            return _from_row(
                venue,
                from_cache=False,
                pages_fetched=list(found.pages_fetched),
            )

    @mcp.tool
    @as_tool_error
    async def suggest_reservation_need(stop_id: uuid.UUID) -> ReservationNeedOut:
        """The default `reservation_need` for a stop, from how its venue books.

        walk_in → none; ticketed → required; resy / opentable / tock /
        sevenrooms / phone → recommended, or required when detection saw
        "reservations required" for a party this stop's size (a rule only
        for parties of 6+ doesn't make a table for two required);
        unknown → unknown. You may raise it; don't
        lower it without a reason. Nothing is written — save it with
        `update_stop`.
        """
        async with open_session() as session:
            stop = await repo.get_stop(session, stop_id)
            venue = _stop_venue(stop)
            need, reason = suggest_need(
                venue.booking_channel, venue.channel_evidence, stop.party_size
            )
            return ReservationNeedOut(
                stop_id=stop.id,
                channel=venue.booking_channel,
                suggested=need,
                reason=reason,
            )

    @mcp.tool
    @as_tool_error
    async def build_booking_link(stop_id: uuid.UUID) -> BookingLinkOut:
        """A reservation link for the user to tap and confirm, for one stop.

        Reads the venue, start time and party size off the stop, so place it
        first (`plan_timeline(apply=True)`). For the online platforms the
        link is prefilled. For a `ticketed` venue it's the venue's own
        booking page, with `verified` false: the user picks the slot there.
        A phone, walk-in or unknown venue raises `UnsupportedChannel` — take
        the phone or walk-in branch instead.

        When `verified` is false, say the link won't (or may not) prefill.
        """
        async with open_session() as session:
            stop = await repo.get_stop(session, stop_id)
            venue = _stop_venue(stop)
            link = links.build(
                venue.link_target,
                _stop_start(stop).astimezone(venue.tz),
                stop.party_size,
            )
        return BookingLinkOut.model_validate(link)

    @mcp.tool
    @as_tool_error
    async def find_google_place(
        venue_id: uuid.UUID, force: bool = False
    ) -> GooglePlaceOut:
        """Look up the venue's Google Place ID and save it on the venue.

        The Resy connector's widget needs this id and refuses a guessed one,
        so never type one in yourself. Searches Google Places for the
        venue's name and address (or city) near its saved coordinates, and
        saves the first result whose name matches and that lies within
        300 m of those coordinates. It fills in an empty lat/lng or address
        from Google while it's there (`filled`). An id already on the venue
        is reused unless `force`.

        A null `google_place_id` means nothing matched: `candidates` shows
        what Google found. If one is clearly the venue (say, a different
        name on Google), say so and save its id with `upsert_venue`;
        otherwise leave it and hand over the link.
        """
        settings = get_settings()
        async with open_session() as session:
            venue = await repo.get_venue(session, venue_id)
            if venue.google_place_id and not force:
                return GooglePlaceOut(
                    venue_id=venue.id,
                    google_place_id=venue.google_place_id,
                    from_cache=True,
                )
            if not settings.google_places_api_key:
                raise PlacesFailed(
                    "GOOGLE_PLACES_API_KEY isn't set in the server's .env, so the "
                    "Resy widget can't be used; hand over build_booking_link."
                )
            near = travel.coordinates(venue) if venue.has_coordinates else None
            found = await places.search(
                f"{venue.name}, {venue.address or venue.city}",
                settings.google_places_api_key,
                near,
            )
            candidates = places.rank(venue.name, near, found)
            match = next((c for c in candidates if c.is_match), None)
            filled: list[str] = []
            if match is not None:
                venue.google_place_id = match.place.place_id
                venue.google_place_data = match.place.raw
                location = match.place.location
                if location is not None and not venue.has_coordinates:
                    venue.lat, venue.lng = location.lat, location.lng
                    filled += ["lat", "lng"]
                if match.place.address and not venue.address:
                    venue.address = match.place.address
                    filled.append("address")
            return GooglePlaceOut(
                venue_id=venue.id,
                google_place_id=venue.google_place_id if match else None,
                filled=filled,
                candidates=[
                    PlaceCandidateOut(
                        google_place_id=c.place.place_id,
                        name=c.place.name,
                        address=c.place.address,
                        lat=c.place.location.lat if c.place.location else None,
                        lng=c.place.location.lng if c.place.location else None,
                        distance_m=(
                            round(c.distance_m) if c.distance_m is not None else None
                        ),
                        is_match=c.is_match,
                    )
                    for c in candidates
                ],
            )

    @mcp.tool
    @as_tool_error
    async def resy_widget_args(
        stop_id: uuid.UUID, now: datetime | None = None
    ) -> ResyWidgetOut:
        """The arguments for the Resy connector's availability widget
        (`display_resy_restaurant_availabilities_app`), for one Resy stop.

        Pass every argument field through unchanged — `google_places_id`,
        `restaurant_name`, `date`, `party_size`, `latitude`, `longitude`,
        `phone_number`. `resy_url` and `local_start` are for you, not the
        widget.

        Raises `UnsupportedChannel` if the venue isn't on Resy,
        `MissingGooglePlace` if `find_google_place` hasn't found its id, and
        `OutsideWidgetRange` when the day is past, more than 60 days out, or
        the party isn't 1–15: the widget can't show those, so hand over
        `build_booking_link` instead. `now` without an offset is the venue's
        local time.
        """
        async with open_session() as session:
            stop = await repo.get_stop(session, stop_id)
            venue = _stop_venue(stop)
            if venue.booking_channel is not BookingChannel.RESY:
                raise UnsupportedChannel(
                    f"{venue.name} books by {venue.booking_channel}, not Resy."
                )
            if not venue.google_place_id:
                raise MissingGooglePlace(
                    f"{venue.name} has no Google Place ID yet; run find_google_place."
                )
            tz = venue.tz
            start = _stop_start(stop).astimezone(tz)
            today = (localize(now, tz) if now else datetime.now(tz)).date()
            check_resy_widget_range(start.date(), today, stop.party_size)
            resy_url = None
            if venue.platform_slug and venue.platform_city_slug:
                resy_url = links.build(venue.link_target, start, stop.party_size).url
            return ResyWidgetOut(
                google_places_id=venue.google_place_id,
                restaurant_name=venue.name,
                date=start.date().isoformat(),
                party_size=stop.party_size,
                latitude=venue.lat,
                longitude=venue.lng,
                phone_number=venue.phone,
                resy_url=resy_url,
                local_start=start,
            )

    @mcp.tool
    @as_tool_error
    async def compute_booking_window(
        stop_id: uuid.UUID, now: datetime | None = None
    ) -> BookingWindowOut:
        """When this stop's venue will start taking a booking for its time.

        `open` means call or book now. `not_yet_open` comes with `opens_at`
        in the venue's timezone — that's when the reminder goes. `unknown`
        means nobody has recorded the venue's window: ask the user or search
        the web, then save it with `upsert_venue`.

        `now` without an offset is read as the venue's local time.
        """
        async with open_session() as session:
            stop = await repo.get_stop(session, stop_id)
            venue = _stop_venue(stop)
            tz = venue.tz
            result = window.compute(
                venue.booking_window,
                _stop_start(stop).astimezone(tz),
                tz,
                localize(now, tz) if now is not None else None,
            )
            return BookingWindowOut(
                state=result.state,
                opens_at=result.opens_at,
                assumed_open_time=result.assumed_open_time,
                window_amount=venue.window_amount,
                window_unit=venue.window_unit,
            )
