"""Tools about venues: what we know, how they book, when they book.

`detect_booking_channel` is the only tool here that reaches out to the
internet, and it caches what it learns on the restaurant row so the next
question is free.
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastmcp import FastMCP

from date_planner.booking import detect as detection_mod
from date_planner.booking import links, window
from date_planner.config import get_settings
from date_planner.db import open_session, repo
from date_planner.db.models import Restaurant
from date_planner.errors import DetectionFailed
from date_planner.schemas import (
    BookingLinkOut,
    BookingWindowOut,
    DetectionOut,
    RestaurantIn,
    RestaurantOut,
)
from date_planner.tools import as_tool_error, localize


def _from_row(
    restaurant: Restaurant,
    *,
    from_cache: bool,
    pages_fetched: list[str] | None = None,
) -> DetectionOut:
    """The restaurant row's own answer about how it books."""
    return DetectionOut(
        restaurant_id=restaurant.id,
        channel=restaurant.booking_channel,
        platform_slug=restaurant.platform_slug,
        platform_city_slug=restaurant.platform_city_slug,
        phone=restaurant.phone,
        confidence=restaurant.channel_confidence or 0.0,
        evidence=restaurant.channel_evidence or "",
        window_amount=restaurant.window_amount,
        window_unit=restaurant.window_unit,
        window_open_time=restaurant.window_open_time,
        from_cache=from_cache,
        pages_fetched=pages_fetched or [],
    )


def _is_fresh(restaurant: Restaurant, ttl_days: int) -> bool:
    checked = restaurant.channel_checked_at
    if checked is None:
        return False
    return datetime.now(timezone.utc) - checked < timedelta(days=ttl_days)


def register(mcp: FastMCP) -> None:
    @mcp.tool
    @as_tool_error
    async def find_restaurant(name: str, city: str) -> RestaurantOut | None:
        """The venue we have on file for this name and city, or null.

        Capitalisation doesn't matter. A null means we've never recorded it:
        follow with `upsert_restaurant`.
        """
        async with open_session() as session:
            row = await repo.find_restaurant(session, name, city)
            return None if row is None else RestaurantOut.model_validate(row)

    @mcp.tool
    @as_tool_error
    async def upsert_restaurant(restaurant: RestaurantIn) -> RestaurantOut:
        """Record a venue, or update one.

        With an `id` it updates that row; without one, `name` and `city`
        identify it, so upserting "Nobu Malibu" twice updates rather than
        duplicates. Anything a person tells you here outranks what detection
        later reads off the website.
        """
        async with open_session() as session:
            return RestaurantOut.model_validate(
                await repo.upsert_restaurant(session, restaurant)
            )

    @mcp.tool
    @as_tool_error
    async def detect_booking_channel(
        restaurant_id: uuid.UUID | None = None,
        website_url: str | None = None,
        force: bool = False,
    ) -> DetectionOut:
        """Read a restaurant's website and work out how it takes reservations.

        Pass `restaurant_id` to detect and cache against a known venue, or
        `website_url` alone to look at a site without recording anything.
        A cached answer is reused until it's 30 days old; `force` re-reads now.

        `confidence` below 0.6 means the site wasn't clear — say so and ask
        the user rather than acting on it. A `phone` channel is the cue to
        compute the booking window and set a reminder; `unknown` means ask.
        """
        settings = get_settings()
        if restaurant_id is None:
            if not website_url:
                raise DetectionFailed("Pass a restaurant_id or a website_url to detect")
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
            restaurant = await repo.get_restaurant(session, restaurant_id)
            if not force and _is_fresh(restaurant, settings.detection_ttl_days):
                return _from_row(restaurant, from_cache=True)
            site = website_url or restaurant.website
            if not site:
                raise DetectionFailed(
                    f"{restaurant.name} has no website on file; add one with "
                    "upsert_restaurant, or set booking_channel by hand."
                )
            found = await detection_mod.detect(site, settings=settings)
            await repo.record_detection(session, restaurant, found)
            # Report the row rather than the raw detection:
            # `record_detection` keeps a phone or window a person typed in.
            return _from_row(
                restaurant,
                from_cache=False,
                pages_fetched=list(found.pages_fetched),
            )

    @mcp.tool
    @as_tool_error
    async def build_booking_link(
        restaurant_id: uuid.UUID, start_at: datetime, party_size: int = 2
    ) -> BookingLinkOut:
        """A prefilled reservation link for the user to tap and confirm.

        `start_at` without an offset is read as the restaurant's local time.
        Only for the online platforms: a phone, walk-in or unknown venue
        raises `UnsupportedChannel`, and that's the cue to take the phone or
        walk-in branch instead.

        When `verified` is false, the platform's URL template hasn't been
        checked against a live venue yet — hand the link over, but say it may
        not prefill.
        """
        async with open_session() as session:
            restaurant = await repo.get_restaurant(session, restaurant_id)
            link = links.build(
                restaurant.link_target,
                localize(start_at, restaurant.tz),
                party_size,
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
        restaurant_id: uuid.UUID,
        target_start_at: datetime,
        now: datetime | None = None,
    ) -> BookingWindowOut:
        """When this restaurant will start taking a booking for that date.

        `open` means call or book now. `not_yet_open` comes with `opens_at`
        in the restaurant's timezone — that's when the reminder goes.
        `unknown` means nobody has recorded the venue's window: ask the user
        or search the web, then save it with `upsert_restaurant`.

        Datetimes without an offset are read as the restaurant's local time.
        """
        async with open_session() as session:
            restaurant = await repo.get_restaurant(session, restaurant_id)
            tz = restaurant.tz
            result = window.compute(
                restaurant.booking_window,
                localize(target_start_at, tz),
                tz,
                localize(now, tz) if now is not None else None,
            )
            return BookingWindowOut(
                state=result.state,
                opens_at=result.opens_at,
                assumed_open_time=result.assumed_open_time,
                window_amount=restaurant.window_amount,
                window_unit=restaurant.window_unit,
            )
