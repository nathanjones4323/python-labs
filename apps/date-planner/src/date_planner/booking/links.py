"""Prefilled reservation links, one builder per platform.

The agent never books: it hands the user a link that lands on the venue's
page with the date, time and party size already filled in, and the user taps
through. Every template here was written from the shape of the platforms'
public URLs, then checked by hand against a live venue —
`scripts/verify_links.py` prints links for that, and confirming one means
adding it to `VERIFIED` with the date it was checked. `build` reports that
flag so the skill can warn if a platform's template ever needs re-checking
(e.g. after the platform changes its URL shape and `VERIFIED` is cleared for
it).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import quote, urlencode, urlparse
from zoneinfo import ZoneInfo

from date_planner.booking.channels import BookingChannel
from date_planner.errors import MissingPlatformSlug, UnsupportedChannel

# Platform -> the day somebody opened a generated link and saw it prefill.
#
# Checked 2026-10-10 against live venues (`scripts/verify_links.py`):
#   - OpenTable (Bavel)        prefilled correctly.
#   - SevenRooms (Catch LA)    prefilled correctly.
#   - Resy (Bestia)            404'd at first — "Venue is inactive or not
#     found" — but that was a stale sample slug, not the template; re-checked
#     against A TÍ (resy.com/cities/los-angeles-ca/venues/a-ti) and it
#     prefilled correctly.
#   - Tock (Kato)              didn't prefill at first because Kato wasn't
#     taking Tock reservations at all, not because of the template;
#     re-checked against Matsumoto and it prefilled correctly.
VERIFIED: dict[BookingChannel, date] = {
    BookingChannel.RESY: date(2026, 10, 10),
    BookingChannel.OPENTABLE: date(2026, 10, 10),
    BookingChannel.TOCK: date(2026, 10, 10),
    BookingChannel.SEVENROOMS: date(2026, 10, 10),
}


@dataclass(frozen=True)
class LinkTarget:
    """What a builder needs about a venue, without the ORM row."""

    channel: BookingChannel
    timezone: ZoneInfo
    platform_slug: str | None = None
    # Resy scopes venues by city (e.g. `los-angeles-ca`).
    platform_city_slug: str | None = None


@dataclass(frozen=True)
class BookingLink:
    url: str
    platform: BookingChannel
    # False while the platform's template is still untested.
    verified: bool
    # The local date and time the link asks for, for the message around it.
    local_start: datetime


def _require(value: str | None, channel: BookingChannel, field: str) -> str:
    if not value:
        raise MissingPlatformSlug(
            f"A {channel} link needs {field}; the venue record has none. "
            "Run detect_booking_channel, or fill it in with upsert_venue."
        )
    return value


def _segment(value: str) -> str:
    """A slug as one path segment — a stray slash can't escape the path."""
    return quote(value, safe="")


def _resy(target: LinkTarget, start: datetime, party_size: int) -> str:
    city = _segment(_require(target.platform_city_slug, target.channel, "a city slug"))
    slug = _segment(_require(target.platform_slug, target.channel, "a venue slug"))
    query = urlencode({"date": start.date().isoformat(), "seats": party_size})
    return f"https://resy.com/cities/{city}/venues/{slug}?{query}"


def _opentable(target: LinkTarget, start: datetime, party_size: int) -> str:
    slug = _segment(_require(target.platform_slug, target.channel, "a venue slug"))
    query = urlencode(
        {"covers": party_size, "dateTime": start.strftime("%Y-%m-%dT%H:%M")}
    )
    return f"https://www.opentable.com/r/{slug}?{query}"


def _tock(target: LinkTarget, start: datetime, party_size: int) -> str:
    slug = _segment(_require(target.platform_slug, target.channel, "a venue slug"))
    query = urlencode(
        {
            "date": start.date().isoformat(),
            "size": party_size,
            "time": start.strftime("%H:%M"),
        }
    )
    return f"https://www.exploretock.com/{slug}/search?{query}"


def _sevenrooms(target: LinkTarget, start: datetime, party_size: int) -> str:
    slug = _segment(_require(target.platform_slug, target.channel, "a venue slug"))
    query = urlencode({"date": start.date().isoformat(), "party_size": party_size})
    return f"https://www.sevenrooms.com/reservations/{slug}?{query}"


Builder = Callable[[LinkTarget, datetime, int], str]

BUILDERS: dict[BookingChannel, Builder] = {
    BookingChannel.RESY: _resy,
    BookingChannel.OPENTABLE: _opentable,
    BookingChannel.TOCK: _tock,
    BookingChannel.SEVENROOMS: _sevenrooms,
}


def _ticket_page(target: LinkTarget) -> str:
    """A `ticketed` venue's own booking page, as detection recorded it.

    Nothing to prefill: museums' ticketing systems don't share a URL shape,
    so the user picks the slot on the page.
    """
    url = _require(target.platform_slug, target.channel, "its booking page URL")
    if urlparse(url).scheme not in ("http", "https"):
        raise MissingPlatformSlug(f"{url!r} isn't a web address")
    return url


def build(target: LinkTarget, start_at: datetime, party_size: int) -> BookingLink:
    """A prefilled link for `start_at`, read in the venue's local time.

    A `ticketed` venue gets its booking page as-is, never `verified`: it
    won't prefill. Raises `UnsupportedChannel` for phone, walk-in and
    unknown — those have no link to build, and the caller should take the
    phone or walk-in branch instead.
    """
    if start_at.tzinfo is None:
        raise ValueError("start_at must be timezone-aware")
    if target.channel is BookingChannel.TICKETED:
        return BookingLink(
            url=_ticket_page(target),
            platform=target.channel,
            verified=False,
            local_start=start_at.astimezone(target.timezone),
        )
    builder = BUILDERS.get(target.channel)
    if builder is None:
        raise UnsupportedChannel(
            f"{target.channel} reservations aren't made through a link; "
            "there's nothing to build."
        )
    local = start_at.astimezone(target.timezone)
    return BookingLink(
        url=builder(target, local, party_size),
        platform=target.channel,
        verified=target.channel in VERIFIED,
        local_start=local,
    )
