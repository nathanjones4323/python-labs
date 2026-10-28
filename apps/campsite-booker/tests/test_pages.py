"""The browser steps against recreation.gov's own pages, trimmed into
`fixtures/pages` from the live site (Upper Pines site 156, Oct 2026). A real
headless Chromium, offline: every request is answered from the fixtures or
aborted. Skipped where Chromium isn't installed."""

import time
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from campsite_booker import booker
from campsite_booker.checkout import CartItem, check_cart, complete_order
from campsite_booker.db import CHALLENGED, DRY_RUN, FAILED, HELD
from campsite_booker.recgov import BASE_URL
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

PAGES = Path(__file__).parent / "fixtures" / "pages"
ROUTES = {
    "/camping/campsites/155": "campsite.html",
    "/camping/reservations/orderdetails": "orderdetails.html",
    "/cart": "cart.html",
}
SUN, MON = date(2026, 11, 8), date(2026, 11, 9)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except PlaywrightError:
            pytest.skip("Chromium isn't installed (playwright install chromium)")
        yield browser
        browser.close()


@pytest.fixture
def site(browser):
    """A page on a fake www.recreation.gov; `.visited` is every page loaded."""
    context = browser.new_context()
    context.set_default_timeout(5_000)
    page = context.new_page()
    page.visited = []

    def answer(route):
        url = urlparse(route.request.url)
        name = ROUTES.get(url.path) if url.hostname == "www.recreation.gov" else None
        if name is None:
            route.abort()
            return
        if route.request.resource_type == "document":
            page.visited.append(route.request.url)
        route.fulfill(content_type="text/html", body=(PAGES / name).read_text())

    page.route("**/*", answer)
    yield page
    context.close()


def go(page, path: str) -> None:
    page.goto(f"{BASE_URL}{path}")


def test_picks_dates_after_clearing_the_ones_the_session_remembered(site):
    go(site, "/camping/campsites/155?remembered")
    booker.pick_dates(site, SUN, MON)
    assert site.evaluate("window.cleared") == 1
    assert site.get_by_text("11/08/2026 - 11/09/2026").is_visible()


def test_the_check_out_day_is_found_after_its_relabelled(site):
    # Picking check-in turns Nov 9 into "Monday, November 9, 2026, Last
    # available date - Checkout"; Nov 8 mustn't match Nov 18 either.
    go(site, "/camping/campsites/155")
    booker.pick_dates(site, SUN, MON)
    assert site.get_by_text("11/08/2026 - 11/09/2026").is_visible()


def test_the_labelled_day_is_picked_not_the_filler(site):
    go(site, "/camping/campsites/155")
    cell = booker._show_day(site, date(2026, 11, 1))
    assert cell.get_attribute("aria-label").endswith("- Available")


def test_a_day_that_cant_be_picked_says_so(site):
    go(site, "/camping/campsites/155")
    with pytest.raises(RuntimeError, match="Current Reservation"):
        booker.pick_dates(site, date(2026, 11, 7), SUN)


def test_add_to_cart_lands_on_the_order_details(site, settings):
    go(site, "/camping/campsites/155")
    booker.pick_dates(site, SUN, MON)
    status, _ = booker.add_to_cart(site, settings)
    assert status == HELD


def test_a_human_check_is_reported_not_answered(site, settings):
    go(site, "/camping/campsites/155?challenge")
    booker.pick_dates(site, SUN, MON)
    started = time.monotonic()
    status, detail = booker.add_to_cart(site, settings)
    assert status == CHALLENGED and "human check" in detail
    # Seen as soon as it shows, not after the 30 s wait for the order page.
    assert time.monotonic() - started < 10
    assert not site.visited[-1].endswith("orderdetails?id=1")


def test_checkout_from_an_incomplete_cart_fills_the_order_details(site, settings):
    go(site, "/cart")
    result = complete_order(site, settings, 4, CartItem("156", SUN))
    assert result.status == DRY_RUN, result.detail
    # The cart sent it to the order details, which got everything they need.
    filled = parse_qs(urlparse(site.visited[-1]).query)
    assert filled == {
        "group": ["4"],
        "vehicles": ["1"],
        "occupant": ["true"],
        "tent": ["true"],
        "rv": ["false"],
        "agreed": ["true"],
    }
    assert site.get_by_text("Review and Confirm").is_visible()


def test_checkout_from_order_details(site, settings):
    go(site, "/camping/reservations/orderdetails?id=1")
    assert complete_order(site, settings, 2).status == DRY_RUN


@pytest.mark.parametrize(
    "item", [CartItem("15", SUN), CartItem("036", SUN), CartItem("156", MON)]
)
def test_a_cart_without_the_site_isnt_paid_for(site, settings, item):
    go(site, "/cart")
    result = complete_order(site, settings, 2, item)
    assert result.status == FAILED and "isn't in your cart" in result.detail
    assert site.visited == [f"{BASE_URL}/cart"]


def test_the_cart_check_matches_site_and_check_in(site):
    go(site, "/cart")
    check_cart(site, CartItem("156", SUN))
