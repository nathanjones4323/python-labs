"""Channel detection, against fixture pages shaped like the real thing.

One fixture per platform, plus a phone-only page modelled on Nobu Malibu's
reservations copy (prose, a `tel:` link, and the window stated in words).
"""

import contextlib
from collections.abc import Iterator
from datetime import time

import pytest
import respx
from date_planner.booking.channels import BookingChannel, WindowUnit
from date_planner.booking.detect import detect, parse_window
from date_planner.config import Settings
from date_planner.errors import DetectionFailed

BASE = "https://venue.example.com"
SETTINGS = Settings(max_pages=4, fetch_timeout=1.0)


def page(body: str, *, nav: str = "") -> str:
    return f"""<!doctype html>
<html><head><title>Venue</title></head>
<body><nav>{nav}</nav>{body}</body></html>"""


HOME_WITH_RESERVATIONS_LINK = page(
    "<h1>Venue</h1>", nav='<a href="/reservations">Reservations</a>'
)

RESY_HOME = page(
    '<a class="book" href="https://resy.com/cities/los-angeles-ca/venues/bestia">'
    "Book a table</a>"
)
OPENTABLE_RESERVATIONS = page(
    '<a href="https://www.opentable.com/r/bavel-los-angeles?ref=1">Reserve</a>'
)
OPENTABLE_RESTREF = page(
    '<iframe src="https://www.opentable.com/restref/client/?rid=123456&amp;'
    'restref=123456"></iframe>'
)
TOCK_RESERVATIONS = page(
    '<a href="https://www.exploretock.com/kato">Reserve on Tock</a>'
    '<footer><a href="https://www.exploretock.com/company">About Tock</a></footer>'
)
SEVENROOMS_RESERVATIONS = page(
    '<div id="widget"><a href="https://www.sevenrooms.com/reservations/catchla">'
    "Reserve</a></div>"
)
RESY_WIDGET_ONLY = page('<script src="https://widgets.resy.com/embed.js"></script>')
# Shaped like Nobu Malibu's real reservations page: an OpenTable widget
# loader with no `/r/<slug>` or `/restref/` link, plus a `tel:` fallback.
OPENTABLE_WIDGET_ONLY = page(
    '<script src="//www.opentable.com/widget/reservation/loader?rid=19252'
    '&domain=com&type=standard"></script>'
    '<a href="tel:+1-310-317-9140">310.317.9140</a>'
)

# Nobu Malibu's reservations page, in spirit: no platform, a phone number,
# and the window spelled out.
PHONE_RESERVATIONS = page(
    """
    <h2>Reservations</h2>
    <p>Reservations are accepted by phone only, one month to the exact
    calendar date. Our reservation lines open at 10:00 am daily.</p>
    <p>Please call <a href="tel:+1-310-317-9140">310.317.9140</a>.
    A credit card is required to hold your table.</p>
    """
)
WALK_IN_HOME = page("<p>We are walk-ins only — no reservations, ever.</p>")
NOTHING_HOME = page("<p>Come visit us. Menus change weekly.</p>")
# The footer says call; the reservations page runs on Resy. Resy wins.
MIXED_HOME = page(
    "<footer>Please call us for a reservation</footer>",
    nav='<a href="/reservations">Reservations</a>',
)


@contextlib.contextmanager
def site(pages: dict[str, str]) -> Iterator[respx.MockRouter]:
    """Serve `pages` (path -> html) and 404 everything else on the host."""
    with respx.mock(assert_all_called=False) as router:
        for path, html in pages.items():
            router.get(f"{BASE}{path}").respond(200, html=html)
        router.route().respond(404)
        yield router


async def test_resy_link_on_the_homepage() -> None:
    with site({"/": RESY_HOME}):
        found = await detect(BASE, settings=SETTINGS)
    assert found.channel is BookingChannel.RESY
    assert found.platform_slug == "bestia"
    assert found.platform_city_slug == "los-angeles-ca"
    assert found.confidence >= 0.9
    assert "resy.com/cities/los-angeles-ca/venues/bestia" in found.evidence


async def test_opentable_on_a_followed_reservations_page() -> None:
    with site(
        {"/": HOME_WITH_RESERVATIONS_LINK, "/reservations": OPENTABLE_RESERVATIONS}
    ):
        found = await detect(BASE, settings=SETTINGS)
    assert found.channel is BookingChannel.OPENTABLE
    assert found.platform_slug == "bavel-los-angeles"
    assert f"{BASE}/reservations" in found.pages_fetched


async def test_opentable_restref_widget_gives_the_rid() -> None:
    with site({"/": OPENTABLE_RESTREF}):
        found = await detect(BASE, settings=SETTINGS)
    assert found.channel is BookingChannel.OPENTABLE
    assert found.platform_slug == "123456"


async def test_tock_ignores_tocks_own_pages() -> None:
    with site({"/": TOCK_RESERVATIONS}):
        found = await detect(BASE, settings=SETTINGS)
    assert found.channel is BookingChannel.TOCK
    assert found.platform_slug == "kato"


async def test_sevenrooms() -> None:
    with site({"/": SEVENROOMS_RESERVATIONS}):
        found = await detect(BASE, settings=SETTINGS)
    assert found.channel is BookingChannel.SEVENROOMS
    assert found.platform_slug == "catchla"


async def test_resy_widget_without_a_slug_is_lower_confidence() -> None:
    with site({"/": RESY_WIDGET_ONLY}):
        found = await detect(BASE, settings=SETTINGS)
    assert found.channel is BookingChannel.RESY
    assert found.platform_slug is None
    assert found.confidence < 0.6  # below the skill's act-on-it threshold


async def test_opentable_widget_without_a_slug_is_below_action_threshold() -> None:
    with site({"/": OPENTABLE_WIDGET_ONLY}):
        found = await detect(BASE, settings=SETTINGS)
    assert found.channel is BookingChannel.OPENTABLE
    assert found.platform_slug is None
    assert found.confidence < 0.6  # nothing build_booking_link can use
    assert found.phone == "+13103179140"


async def test_phone_only_page_gives_channel_phone_number_and_window() -> None:
    with site({"/": PHONE_RESERVATIONS}):
        found = await detect(BASE, settings=SETTINGS)
    assert found.channel is BookingChannel.PHONE
    assert found.phone == "+13103179140"
    assert found.window is not None
    # "One month to the exact calendar date" is a month, not 30 days.
    assert found.window.amount == 1
    assert found.window.unit is WindowUnit.MONTHS
    assert found.window.open_time == time(10, 0)


async def test_walk_in_only() -> None:
    with site({"/": WALK_IN_HOME}):
        found = await detect(BASE, settings=SETTINGS)
    assert found.channel is BookingChannel.WALK_IN


async def test_nothing_to_go_on_is_unknown() -> None:
    with site({"/": NOTHING_HOME}):
        found = await detect(BASE, settings=SETTINGS)
    assert found.channel is BookingChannel.UNKNOWN
    assert found.confidence == 0.0


async def test_a_platform_beats_prose_about_calling() -> None:
    with site({"/": MIXED_HOME, "/reservations": RESY_HOME}):
        found = await detect(BASE, settings=SETTINGS)
    assert found.channel is BookingChannel.RESY


async def test_at_most_max_pages_are_fetched() -> None:
    with site({"/": HOME_WITH_RESERVATIONS_LINK}) as router:
        await detect(BASE, settings=Settings(max_pages=2, fetch_timeout=1.0))
        # The homepage and one reservation page, and then it stops.
        assert len(router.calls) == 2


async def test_an_unreachable_site_fails_loudly() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.route().respond(503)
        with pytest.raises(DetectionFailed):
            await detect(BASE, settings=SETTINGS)


async def test_a_subpage_404_doesnt_stop_detection() -> None:
    with site({"/": RESY_HOME}):  # /reservations etc. all 404
        found = await detect(BASE, settings=SETTINGS)
    assert found.channel is BookingChannel.RESY


async def test_an_empty_website_is_refused() -> None:
    with pytest.raises(DetectionFailed):
        await detect("   ", settings=SETTINGS)


@pytest.mark.parametrize(
    ("text", "amount", "unit", "open_time"),
    [
        ("Book up to 30 days in advance.", 30, WindowUnit.DAYS, None),
        ("Reservations open 60 days ahead", 60, WindowUnit.DAYS, None),
        ("We take bookings two weeks in advance", 14, WindowUnit.DAYS, None),
        ("Tables are released one month in advance", 1, WindowUnit.MONTHS, None),
        (
            "One month to the exact calendar date. Lines open at 10 am.",
            1,
            WindowUnit.MONTHS,
            time(10, 0),
        ),
        (
            "Up to 7 days in advance; reservations open at 9:30 a.m.",
            7,
            WindowUnit.DAYS,
            time(9, 30),
        ),
        ("Bookings open at 6 p.m. 14 days prior", 14, WindowUnit.DAYS, time(18, 0)),
    ],
)
def test_parse_window(
    text: str, amount: int, unit: WindowUnit, open_time: time | None
) -> None:
    window = parse_window(text)
    assert window is not None
    assert (window.amount, window.unit, window.open_time) == (
        amount,
        unit,
        open_time,
    )


def test_parse_window_finds_nothing_in_ordinary_copy() -> None:
    assert parse_window("Open Tuesday to Sunday, 5pm until late.") is None
