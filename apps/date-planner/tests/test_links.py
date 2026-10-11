"""Deep links: exact URLs, and the channels that have none.

These are snapshots on purpose. The templates are guesses until somebody
opens them (see `scripts/verify_links.py`), so a change to one should be a
deliberate edit to a test, not a silent drift.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from date_planner.booking.channels import BookingChannel
from date_planner.booking.links import BUILDERS, VERIFIED, LinkTarget, build
from date_planner.errors import MissingPlatformSlug, UnsupportedChannel

LA = ZoneInfo("America/Los_Angeles")
NY = ZoneInfo("America/New_York")
SATURDAY_7PM = datetime(2026, 10, 24, 19, 0, tzinfo=LA)

RESY = LinkTarget(
    channel=BookingChannel.RESY,
    timezone=LA,
    platform_slug="bestia",
    platform_city_slug="los-angeles-ca",
)
OPENTABLE = LinkTarget(
    channel=BookingChannel.OPENTABLE, timezone=LA, platform_slug="bavel-los-angeles"
)
TOCK = LinkTarget(channel=BookingChannel.TOCK, timezone=LA, platform_slug="kato")
SEVENROOMS = LinkTarget(
    channel=BookingChannel.SEVENROOMS, timezone=LA, platform_slug="catchla"
)


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (
            RESY,
            "https://resy.com/cities/los-angeles-ca/venues/bestia"
            "?date=2026-10-24&seats=2",
        ),
        (
            OPENTABLE,
            "https://www.opentable.com/r/bavel-los-angeles"
            "?covers=2&dateTime=2026-10-24T19%3A00",
        ),
        (
            TOCK,
            "https://www.exploretock.com/kato/search"
            "?date=2026-10-24&size=2&time=19%3A00",
        ),
        (
            SEVENROOMS,
            "https://www.sevenrooms.com/reservations/catchla"
            "?date=2026-10-24&party_size=2",
        ),
    ],
)
def test_url_for_each_platform(target: LinkTarget, expected: str) -> None:
    assert build(target, SATURDAY_7PM, 2).url == expected


def test_party_size_is_passed_through() -> None:
    assert "seats=4" in build(RESY, SATURDAY_7PM, 4).url


def test_time_is_the_restaurants_local_time() -> None:
    # 22:00 in New York is 19:00 in Los Angeles, and the link is for 19:00.
    eastern = datetime(2026, 10, 24, 22, 0, tzinfo=NY)
    link = build(OPENTABLE, eastern, 2)
    assert "dateTime=2026-10-24T19%3A00" in link.url
    assert link.local_start == SATURDAY_7PM


def test_a_slug_with_a_slash_cannot_escape_the_path() -> None:
    sneaky = LinkTarget(
        channel=BookingChannel.TOCK, timezone=LA, platform_slug="kato/../evil"
    )
    assert "kato%2F..%2Fevil" in build(sneaky, SATURDAY_7PM, 2).url


@pytest.mark.parametrize(
    "channel",
    [BookingChannel.PHONE, BookingChannel.WALK_IN, BookingChannel.UNKNOWN],
)
def test_offline_channels_have_no_link(channel: BookingChannel) -> None:
    with pytest.raises(UnsupportedChannel):
        build(LinkTarget(channel=channel, timezone=LA), SATURDAY_7PM, 2)


def test_resy_without_a_city_slug_says_what_is_missing() -> None:
    target = LinkTarget(
        channel=BookingChannel.RESY, timezone=LA, platform_slug="bestia"
    )
    with pytest.raises(MissingPlatformSlug, match="city slug"):
        build(target, SATURDAY_7PM, 2)


def test_online_channel_without_a_slug_is_an_error() -> None:
    target = LinkTarget(channel=BookingChannel.OPENTABLE, timezone=LA)
    with pytest.raises(MissingPlatformSlug, match="venue slug"):
        build(target, SATURDAY_7PM, 2)


def test_a_naive_start_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build(RESY, datetime(2026, 10, 24, 19, 0), 2)


def test_verified_reflects_the_registry() -> None:
    # All four checked 2026-10-10 against live venues (see the note above
    # `links.VERIFIED`) and prefilled correctly.
    assert set(VERIFIED) == set(BUILDERS)
    assert build(RESY, SATURDAY_7PM, 2).verified is True
    assert build(OPENTABLE, SATURDAY_7PM, 2).verified is True
    assert build(TOCK, SATURDAY_7PM, 2).verified is True
    assert build(SEVENROOMS, SATURDAY_7PM, 2).verified is True


def test_a_ticketed_venue_gets_its_own_page_unverified() -> None:
    target = LinkTarget(
        channel=BookingChannel.TICKETED,
        timezone=LA,
        platform_slug="https://tickets.getty.edu/online/article/villa",
    )
    link = build(target, datetime(2026, 10, 24, 16, 0, tzinfo=NY), 2)
    assert link.url == "https://tickets.getty.edu/online/article/villa"
    assert link.verified is False
    assert link.local_start == datetime(2026, 10, 24, 13, 0, tzinfo=LA)


@pytest.mark.parametrize("slug", [None, "javascript:alert(1)"])
def test_a_ticketed_venue_needs_a_web_address(slug: str | None) -> None:
    target = LinkTarget(
        channel=BookingChannel.TICKETED, timezone=LA, platform_slug=slug
    )
    with pytest.raises(MissingPlatformSlug):
        build(target, SATURDAY_7PM, 2)
