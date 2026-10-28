"""Putting a site in your recreation.gov cart, with a real browser.

recreation.gov has no public booking API, so this drives the site's own
page the way you would: open the campsite, pick the check-in and check-out
days on its calendar, press **Add to Cart**. That reserves the site for you
for the cart's 15 minutes. Asked to pay (`pay_group_size`), it carries on
through checkout with the card saved on the account (`checkout.py`);
otherwise you finish the order on recreation.gov. Nothing here types a card
number.

Signing in is behind reCAPTCHA, which a headless browser with a fresh
profile tends to fail. So the session is signed in once in a visible window
(`campsite-booker-login`), saved to `Settings.session_file` (cookies and
local storage) and reused by every hold, which saves it again afterwards so
refreshed tokens are kept. If the saved session has expired, a hold tries
the login form with `RECGOV_EMAIL` / `RECGOV_PASSWORD`; if that's refused
too, it stops with `LoginRequired` and you run the login again.

The watcher, the MCP server and the CLI are separate processes on that one
session file, so every browser run takes `session_lock` first: two at once
would each refresh the tokens and the last to save would sign the other out.

Selectors anchor on what the page's own accessibility depends on: calendar
days are `role=button` with labels like "Sunday, November 1, 2026 -
Available", the login form's inputs are `#email` / `#password`, and buttons
are found by their text.
"""

import fcntl
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from loguru import logger
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    sync_playwright,
)
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from campsite_booker.checkout import CartItem, CheckoutResult, complete_order, settle
from campsite_booker.config import Settings
from campsite_booker.db import CHALLENGED, FAILED, HELD, UNKNOWN
from campsite_booker.recgov import BASE_URL, CART_URL, Stay

STEP_TIMEOUT_MS = 15_000
# The calendar shows two months; at most this many "Next" presses to reach
# the check-in month (the furthest any campground books ahead is ~12).
MAX_MONTH_STEPS = 14
# After Add to Cart: how long to wait for the order page or an error.
RESULT_TIMEOUT_MS = 30_000
# The modal recreation.gov shows after Add to Cart when it wants a reCAPTCHA
# answered before booking.
HUMAN_CHECK = "We need to verify that you're a human"
# How long a visible window waits for you to answer it.
HUMAN_CHECK_TIMEOUT_MS = 180_000
# The header's "Sign Up / Log In" button, shown only while signed out. CSS
# rather than a role: with the login modal open the header is aria-hidden,
# and a role lookup would miss the button and take you for signed in.
LOGIN_BUTTON = '#ga-global-nav-log-in-link, button[aria-label="Sign Up or Log In"]'


class LoginRequired(RuntimeError):
    """The saved session has expired and the login form was refused (most
    likely reCAPTCHA). Run `campsite-booker-login`."""


@dataclass(frozen=True)
class HoldResult:
    status: str  # HELD / FAILED / UNKNOWN
    detail: str
    page_url: str | None = None
    # A screenshot of where it stopped, on failure.
    screenshot: Path | None = None
    # How paying went, when it was asked for and the hold worked.
    checkout: CheckoutResult | None = None


def user_agent(browser: Browser) -> str:
    """A normal desktop Chrome UA for the launched Chromium's version. The
    default says "HeadlessChrome", and an older version gets recreation.gov's
    "outdated browser" banner over the page."""
    major = browser.version.split(".")[0]
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def new_context(browser: Browser, session_file: Path) -> BrowserContext:
    context = browser.new_context(
        user_agent=user_agent(browser),
        viewport={"width": 1300, "height": 1000},
        locale="en-US",
        storage_state=str(session_file) if session_file.exists() else None,
    )
    context.set_default_timeout(STEP_TIMEOUT_MS)
    return context


@contextmanager
def session_lock(settings: Settings) -> Iterator[None]:
    """Wait for any other process's browser run on the saved session to
    finish, and keep it to this one until the block ends. An OS file lock,
    so it's let go if the process dies."""
    path = settings.session_file.with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Worth knowing during a release: the hold is queued behind this.
            logger.info("Waiting for another browser run on the session to finish")
            started = time.monotonic()
            fcntl.flock(lock, fcntl.LOCK_EX)
            logger.info(f"Got the session after {time.monotonic() - started:.0f}s")
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def save_session(context: BrowserContext, session_file: Path) -> None:
    session_file.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(session_file))
    os.chmod(session_file, 0o600)


def open_page(page: Page, url: str) -> None:
    """Go to `url` and let it finish loading. Not "networkidle": the home
    page never goes quiet, so that times out."""
    page.goto(url, wait_until="load", timeout=45_000)
    settle(page)


def calendar_label(day: date) -> str:
    """How the calendar labels a day, up to its status: "Sunday, November 1,
    2026"."""
    return f"{day.strftime('%A, %B')} {day.day}, {day.year}"


def is_logged_in(page: Page) -> bool:
    return page.locator(LOGIN_BUTTON).count() == 0


def log_in(page: Page, settings: Settings) -> None:
    """Fill the login modal (opening it first if it isn't showing) and wait
    for the site to accept it."""
    if not (settings.recgov_email and settings.recgov_password):
        raise LoginRequired(
            "Not signed in to recreation.gov and RECGOV_EMAIL / RECGOV_PASSWORD "
            "aren't set; run campsite-booker-login"
        )
    email = page.locator("#email")
    if not email.is_visible():
        page.locator(LOGIN_BUTTON).first.click()
        email.wait_for(state="visible")
    email.fill(settings.recgov_email)
    page.locator("#password").fill(settings.recgov_password)
    try:
        with page.expect_response(
            lambda r: "/api/accounts/login" in r.url and r.request.method == "POST",
            timeout=STEP_TIMEOUT_MS,
        ) as response_info:
            page.get_by_role("button", name="Log In", exact=True).click()
        response = response_info.value
    except PlaywrightTimeout:
        raise LoginRequired(
            "recreation.gov didn't answer the login (reCAPTCHA?); "
            "run campsite-booker-login"
        ) from None
    if not response.ok:
        raise LoginRequired(
            f"recreation.gov refused the login ({response.status}); check "
            "RECGOV_EMAIL / RECGOV_PASSWORD, or run campsite-booker-login"
        )
    email.wait_for(state="hidden")
    logger.info("Signed in to recreation.gov")


def pick_dates(page: Page, check_in: date, check_out: date) -> None:
    """Click check-in then check-out on the site's availability calendar,
    paging it forward to the right month first. The site keeps the last
    dates picked (in the saved session's local storage), so those are
    cleared first."""
    clear = page.get_by_role("button", name="Clear Dates")
    if clear.count() and clear.first.is_visible():
        clear.first.click()
        page.get_by_text(
            re.compile(r"\d\d/\d\d/\d{4} - \d\d/\d\d/\d{4}")
        ).first.wait_for(state="hidden")
    first = _show_day(page, check_in)
    label = first.get_attribute("aria-label") or ""
    if first.get_attribute("aria-disabled") == "true":
        raise RuntimeError(f"Check-in day isn't selectable: {label!r}")
    first.click()
    _show_day(page, check_out).click()
    # The bar along the bottom echoes the chosen dates.
    page.get_by_text(
        f"{check_in.strftime('%m/%d/%Y')} - {check_out.strftime('%m/%d/%Y')}"
    ).first.wait_for()


def _show_day(page: Page, day: date) -> Locator:
    """The calendar's cell for `day`, paging forward until it's in a month
    on show. Only a shown month's days carry their status ("Friday, January
    1, 2027 - Available"); the same day as filler at the end of the month
    before is unlabelled and disabled, so it's the labelled one we want.
    Once check-in is picked, days are labelled around it ("Monday, November
    9, 2026, Last available date - Checkout"), so it's matched on the date
    alone, anywhere in the label: the weekday before it and the year after
    keep November 8 from matching November 18."""
    label = calendar_label(day)
    cell = page.locator(
        f'[role="button"][aria-label*="{label}"]:not([aria-label="{label}"])'
    )
    for _ in range(MAX_MONTH_STEPS):
        if cell.count():
            return cell.first
        page.get_by_role("button", name="Next", exact=True).first.click()
        try:
            # The month's availability arrives a moment after the page turn.
            cell.first.wait_for(timeout=1_500)
        except PlaywrightTimeout:
            pass
    raise RuntimeError(f"{day} isn't on the site's calendar")


def _on_order_page(url: str) -> bool:
    return "/orderdetails" in url or "/cart" in url or "/reservations/" in url


def add_to_cart(page: Page, settings: Settings) -> tuple[str, str]:
    """Press Add to Cart, signing in if the site asks, and say how it went:
    (status, detail)."""
    button = page.get_by_role("button", name="Add to Cart")
    button.click()
    # Signed out, the site answers Add to Cart with its login modal.
    try:
        page.locator("#email").wait_for(state="visible", timeout=2_500)
        log_in(page, settings)
        if not _on_order_page(page.url) and button.is_visible():
            button.click()
    except PlaywrightTimeout:
        pass
    challenge = page.get_by_text(HUMAN_CHECK)
    deadline = time.monotonic() + RESULT_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        if _on_order_page(page.url):
            return HELD, "In your cart for 15 minutes"
        if challenge.count() and challenge.first.is_visible():
            return _human_check(page, settings)
        page.wait_for_timeout(250)
    error = _visible_error(page)
    if error:
        return FAILED, error
    return UNKNOWN, (
        "Pressed Add to Cart but neither the order page nor an error came "
        "up; check the cart on recreation.gov"
    )


def _human_check(page: Page, settings: Settings) -> tuple[str, str]:
    """recreation.gov's "verify that you're a human" reCAPTCHA, shown after
    Add to Cart. Only a person can answer it, so nothing here tries: in a
    visible window it waits for you to; headless, it stops and says so (you
    add it on recreation.gov, and it can be paid from the cart after)."""
    if settings.headless:
        return CHALLENGED, (
            "recreation.gov asked for a human check (reCAPTCHA) before it would "
            "go in the cart"
        )
    logger.info("recreation.gov wants a human check: solve it in the window")
    print("Solve recreation.gov's human check in the window, then Continue Booking")
    try:
        page.wait_for_url(
            lambda url: _on_order_page(url), timeout=HUMAN_CHECK_TIMEOUT_MS
        )
    except PlaywrightTimeout:
        return CHALLENGED, "recreation.gov's human check wasn't answered in time"
    return HELD, "In your cart for 15 minutes"


def _visible_error(page: Page) -> str | None:
    for alert in page.locator('[role="alert"], .sarsa-alert, .rec-notification').all():
        try:
            text = alert.inner_text(timeout=1_000).strip()
        except PlaywrightTimeout:
            continue
        if text:
            return " ".join(text.split())[:300]
    return None


def hold(
    settings: Settings, stay: Stay, pay_group_size: int | None = None
) -> HoldResult:
    """Put `stay` in the signed-in account's cart, and with `pay_group_size`
    go on to pay for it (a dry run unless `settings.submit_payment`). Never
    raises for the site's reasons: a `LoginRequired` is the one error that
    escapes, since only you can fix it."""
    if not stay.holdable:
        return HoldResult(
            FAILED,
            "State park sites can't be held from here: book it on ReserveCalifornia",
            stay.url,
        )
    with session_lock(settings), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=settings.headless)
        context = new_context(browser, settings.session_file)
        page = context.new_page()
        try:
            open_page(page, stay.url)
            if not is_logged_in(page) and settings.recgov_email:
                # Sign in before the race rather than in the middle of it.
                log_in(page, settings)
            pick_dates(page, stay.check_in, stay.check_out)
            status, detail = add_to_cart(page, settings)
            order_url = page.url
            screenshot = checkout = None
            if status != HELD:
                screenshot = _artifacts(page, settings, stay, status)
            elif pay_group_size is not None:
                checkout = complete_order(page, settings, pay_group_size)
                screenshot = _artifacts(page, settings, stay, f"pay-{checkout.status}")
            return HoldResult(status, detail, order_url, screenshot, checkout)
        except LoginRequired:
            _artifacts(page, settings, stay, "login")
            raise
        except Exception as e:
            logger.exception(f"Holding {stay.campsite_id} failed")
            return HoldResult(
                FAILED,
                f"{type(e).__name__}: {e}"[:300],
                page.url,
                _artifacts(page, settings, stay, "error"),
            )
        finally:
            try:
                if is_logged_in(page):
                    save_session(context, settings.session_file)
            except Exception:
                logger.exception("Saving the recreation.gov session failed")
            browser.close()


def pay(
    settings: Settings,
    order_url: str | None,
    group_size: int,
    label: str,
    expect: CartItem | None = None,
) -> tuple[CheckoutResult, Path | None]:
    """Pay for a site held earlier: from its order details page if the hold
    recorded one, else from the cart, and then only if the cart has `expect`.
    Raises `LoginRequired` like `hold`."""
    with session_lock(settings), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=settings.headless)
        context = new_context(browser, settings.session_file)
        page = context.new_page()
        try:
            start = order_url if order_url and "orderdetails" in order_url else CART_URL
            open_page(page, start)
            if not is_logged_in(page):
                log_in(page, settings)
                open_page(page, start)
            result = complete_order(page, settings, group_size, expect)
            return result, _artifacts(page, settings, label, f"pay-{result.status}")
        finally:
            try:
                if is_logged_in(page):
                    save_session(context, settings.session_file)
            except Exception:
                logger.exception("Saving the recreation.gov session failed")
            browser.close()


def _artifacts(
    page: Page, settings: Settings, stay: Stay | str, what: str
) -> Path | None:
    """A screenshot and the page HTML: the HTML is what repairs a selector.
    Card and personal inputs are masked in the screenshot."""
    name = (
        stay
        if isinstance(stay, str)
        else (f"{stay.campsite_id}-{stay.check_in.isoformat()}")
    )
    stem = f"{name}-{what}"
    try:
        settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        shot = settings.artifacts_dir / f"{stem}.png"
        page.screenshot(
            path=str(shot),
            full_page=True,
            mask=[page.locator("input, select, iframe")],
        )
        (settings.artifacts_dir / f"{stem}.html").write_text(page.content())
        return shot
    except Exception as e:  # never mask the original error
        logger.warning(f"Couldn't save artifacts: {e}")
        return None


def interactive_login(settings: Settings, timeout_s: int = 300) -> None:
    """Open a visible browser on recreation.gov's login, fill in the login
    from the environment if it's there, and wait for you to finish (solve a
    challenge if one comes up). Saves the session for `hold`."""
    with session_lock(settings), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = new_context(browser, settings.session_file)
        page = context.new_page()
        open_page(page, BASE_URL)
        if is_logged_in(page):
            logger.info("Already signed in")
        else:
            try:
                # The site may have opened the login modal (or another one
                # over the header) by itself, so only click when it isn't up.
                email = page.locator("#email")
                if not email.is_visible():
                    page.locator(LOGIN_BUTTON).first.click()
                    email.wait_for(state="visible")
                if settings.recgov_email:
                    email.fill(settings.recgov_email)
                if settings.recgov_password:
                    page.locator("#password").fill(settings.recgov_password)
                    page.get_by_role("button", name="Log In", exact=True).click()
            except PlaywrightTimeout:
                # Something's in the way: you can still sign in by hand.
                logger.info("Couldn't open the login form; use the window")
            print("Finish signing in in the browser window (it waits 5 minutes)...")
            page.locator(LOGIN_BUTTON).first.wait_for(
                state="detached", timeout=timeout_s * 1000
            )
            # Let the site store its tokens before saving them.
            settle(page)
        save_session(context, settings.session_file)
        browser.close()


def check_session(settings: Settings) -> bool:
    """Whether the saved session is still signed in (headless, no login)."""
    with session_lock(settings), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = new_context(browser, settings.session_file)
        page = context.new_page()
        open_page(page, BASE_URL)
        signed_in = is_logged_in(page)
        browser.close()
        return signed_in
