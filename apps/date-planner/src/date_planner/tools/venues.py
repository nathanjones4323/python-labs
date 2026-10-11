"""Tools about venues: what we know, how they book, when they book.

`detect_booking_channel` is the only tool here that reaches out to the
internet, and it caches what it learns on the venue row so the next question
is free. The booking tools take a stop, not a venue: the stop says when and
for how many.
"""

import re
import uuid
from datetime import datetime, timedelta, timezone

from fastmcp import FastMCP

from date_planner.booking import detect as detection_mod
from date_planner.booking import links, window
from date_planner.booking.channels import ONLINE_CHANNELS, BookingChannel
from date_planner.config import get_settings
from date_planner.db import open_session, repo
from date_planner.db.models import DateStop, ReservationNeed, Venue
from date_planner.errors import DetectionFailed, IncompleteStop
from date_planner.schemas import (
    BookingLinkOut,
    BookingWindowOut,
    DetectionOut,
    ReservationNeedOut,
    VenueIn,
    VenueOut,
)
from date_planner.tools import as_tool_error, localize

REQUIRED_EVIDENCE = re.compile(re.escape(detection_mod.REQUIRED_NOTE), re.I)


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


def suggest_need(
    channel: BookingChannel, evidence: str | None
) -> tuple[ReservationNeed, str]:
    """The default reservation need for a venue that books through `channel`."""
    if channel is BookingChannel.WALK_IN:
        return ReservationNeed.NONE, "Walk-in only: nothing to book."
    if channel is BookingChannel.TICKETED:
        return ReservationNeed.REQUIRED, "Timed entry: no ticket, no entry."
    if channel in ONLINE_CHANNELS or channel is BookingChannel.PHONE:
        if evidence and REQUIRED_EVIDENCE.search(evidence):
            return (
                ReservationNeed.REQUIRED,
                f"Books by {channel}, and the site says reservations are required.",
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
            found = await detection_mod.detect(website_url, settings=settings)
            return DetectionOut(
                channel=found.channel,
                platform_slug=found.platform_slug,
                platform_city_slug=found.platform_city_slug,
                phone=found.phone,
                confidence=found.confidence,
                evidence=found.evidence,
                window_amount=found.window.amount if found.window else None,
                window_unit=found.window.unit if found.window else None,
                window_open_time=(found.window.open_time if found.window else None),
                pages_fetched=list(found.pages_fetched),
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
        "reservations required"; unknown → unknown. You may raise it; don't
        lower it without a reason. Nothing is written — save it with
        `update_stop`.
        """
        async with open_session() as session:
            stop = await repo.get_stop(session, stop_id)
            venue = _stop_venue(stop)
            need, reason = suggest_need(venue.booking_channel, venue.channel_evidence)
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
        return BookingLinkOut(
            url=link.url,
            platform=link.platform,
            verified=link.verified,
            local_start=link.local_start,
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
