"""Drives foreUP's booking UI for Soule Park with Playwright.

Flow (mirrors what a person does on the site):
  1. open the booking page and pick the public booking class
  2. log in
  3. fetch the day's slots from the API using the browser's logged-in session
     and choose one per our preferences
  4. set the date, click the slot, choose holes (if asked) and players,
     "Book Time", confirm
  5. fill the card (Basis Theory hosted iframe) and postal code
  6. optionally click Submit
"""

import random
import re
import time
import urllib.parse
from datetime import date
from pathlib import Path

from loguru import logger
from playwright.sync_api import BrowserContext, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tee_time_booker import config
from tee_time_booker.config import Credentials, Settings
from tee_time_booker.tee_times import (
    TeeTime,
    api_query_params,
    choose_tee_time,
    parse_tee_times,
)

# How long to wait for any single element before giving up.
STEP_TIMEOUT_MS = 15_000

# --- Selectors ------------------------------------------------------------------
PUBLIC_BOOKING_CLASS_BUTTON = "#content > div > button:nth-child(2)"
NAV_LOGIN_LINK = "#bs-example-navbar-collapse-1 ul.navbar-right.visible-lg li a"
LOGIN_EMAIL = "#login_email"
LOGIN_PASSWORD = "#login_password"
LOGIN_SUBMIT = "#login .modal-footer button.btn-primary.login"
DATE_FIELD = "#date-field"
SLOT_TIME_LABEL = "div.booking-start-time-label"
BOOKING_MODAL = "#book_time"
HOLES_BUTTON = f"#book_time [data-field='holes'] a[data-value='{config.HOLES}']"
PLAYERS_BUTTON = f"#book_time [data-field='players'] a[data-value='{config.PLAYERS}']"
BOOK_TIME_BUTTON = "#book_time .modal-footer button.js-book-button"
CONFIRM_BOOKING_BUTTON = "#payment_selection .modal-footer button.continue"
# Payment is an OdinPay form built on Basis Theory hosted elements: one iframe
# holds number / expiry / CVC; postal code is a plain input. No name field.
CARD_IFRAME = "#payment_selection iframe[src*='card-element']"
CARD_NUMBER_INPUT = "input[autocomplete='cc-number']"
CARD_EXP_INPUT = "input[autocomplete='cc-exp']"
CARD_CVC_INPUT = "input[autocomplete='cc-csc']"
POSTAL_INPUT = "#payment_selection input.odin-input[placeholder='Postal Code']"
SUBMIT_PAYMENT_BUTTON = "#payment_selection button#submit"


def _user_agent(browser_version: str) -> str:
    """A normal desktop Chrome UA matching the launched Chromium's major version.

    foreUP returns 403 to the default headless UA ("HeadlessChrome/...").
    """
    major = browser_version.split(".")[0]
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def _pause(low: float = 0.4, high: float = 1.2) -> None:
    """Small human-ish delay between UI steps."""
    time.sleep(random.uniform(low, high))


def booking_page_url(target: date) -> str:
    params = urllib.parse.urlencode(api_query_params(target), doseq=True)
    return f"{config.BOOKING_URL}#/teetimes?{params}"


def fetch_tee_times(context: BrowserContext, target: date) -> list[TeeTime]:
    """Call the slots API via the browser context so it carries the login cookies."""
    params = api_query_params(target)
    logger.info(f"Fetching tee times for {target:%A %Y-%m-%d} with {params}")
    response = context.request.get(config.TEE_TIMES_API_URL, params=params)
    if not response.ok:
        raise RuntimeError(
            f"Tee time API returned {response.status}: {response.text()[:500]}"
        )
    tee_times = parse_tee_times(response.json())
    logger.info(f"API returned {len(tee_times)} tee times")
    if not tee_times:
        raise RuntimeError(f"No tee times available on {target}")
    return tee_times


def open_booking_page(page: Page, target: date) -> None:
    page.goto(booking_page_url(target), wait_until="domcontentloaded")
    page.click(PUBLIC_BOOKING_CLASS_BUTTON)
    _pause()


def login(page: Page, creds: Credentials) -> None:
    page.click(NAV_LOGIN_LINK)
    page.wait_for_selector("#modal", state="visible")
    page.fill(LOGIN_EMAIL, creds.email)
    _pause()
    page.fill(LOGIN_PASSWORD, creds.password)
    _pause()
    page.click(LOGIN_SUBMIT)
    # The login modal closing is the only reliable signal the session is live.
    page.wait_for_selector("#login", state="hidden")
    logger.info("Logged in")
    _pause()


def set_date(page: Page, target: date) -> None:
    field = page.locator(DATE_FIELD)
    field.click()
    field.fill("")
    field.type(target.strftime(config.FOREUP_DATE_FORMAT))
    field.press("Enter")
    # The slot list re-renders after the date changes; let it settle.
    page.wait_for_load_state("networkidle")
    _pause(0.5, 1.0)


def select_slot(page: Page, tee_time: TeeTime, target: date) -> None:
    label = tee_time.ui_label
    # Anchor on digit boundaries so "1:00pm" can't match "11:00pm".
    pattern = re.compile(rf"(?<!\d){re.escape(label)}(?!\w)", re.IGNORECASE)
    slot = page.locator(SLOT_TIME_LABEL, has_text=pattern).first
    # foreUP rejects the hold (HTTP 400, no modal) if the account already has a
    # pending reservation, and a click can land mid-re-render. Confirm the
    # modal opened; otherwise refresh the list and retry.
    for attempt in range(1, 4):
        if attempt > 1:
            _pause(2.0, 4.0)
            set_date(page, target)
        slot.wait_for(state="visible")
        slot.click()
        try:
            page.wait_for_selector(BOOKING_MODAL, state="visible", timeout=5_000)
            logger.info(f"Clicked slot {label}")
            _pause()
            return
        except PlaywrightTimeoutError:
            logger.warning(f"Slot click {attempt} did not open the booking modal")
    raise RuntimeError(f"Could not open the booking modal for slot {label}")


def choose_players_and_book(page: Page) -> None:
    # Slots offered as "9 or 18" show a holes picker; 18-only slots don't.
    holes = page.locator(HOLES_BUTTON)
    if holes.count():
        holes.click()
        _pause()
    page.click(PLAYERS_BUTTON)
    _pause()
    page.click(BOOK_TIME_BUTTON)
    _pause()
    page.click(CONFIRM_BOOKING_BUTTON)
    _pause()


def fill_payment_details(page: Page, creds: Credentials) -> None:
    card = page.frame_locator(CARD_IFRAME)
    # Hosted inputs format as you type, so send keystrokes rather than fill().
    fields = [
        (card.locator(CARD_NUMBER_INPUT), creds.card_number),
        (
            card.locator(CARD_EXP_INPUT),
            creds.card_expiration_month + creds.card_expiration_year,
        ),
        (card.locator(CARD_CVC_INPUT), creds.card_cvc),
        (page.locator(POSTAL_INPUT), creds.card_postal_code),
    ]
    for locator, value in fields:
        locator.click()
        locator.press_sequentially(value, delay=40)
        _pause(0.2, 0.6)
    logger.info("Filled payment details")


def submit_payment(page: Page) -> None:
    page.click(SUBMIT_PAYMENT_BUTTON)
    # Give the processor time to redirect / render the confirmation.
    page.wait_for_load_state("networkidle", timeout=30_000)
    logger.info("Submitted payment")


def book_tee_time(
    target: date, creds: Credentials, settings: Settings, run_id: str
) -> TeeTime:
    """Run the full flow. Returns the booked slot; raises on any failure.

    On failure a screenshot is written to settings.artifacts_dir for debugging.
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=settings.headless)
        context = browser.new_context(
            viewport={"width": 1400, "height": 1000},
            timezone_id=str(config.COURSE_TZ),
            user_agent=_user_agent(browser.version),
        )
        context.set_default_timeout(STEP_TIMEOUT_MS)
        page = context.new_page()
        try:
            open_booking_page(page, target)
            login(page, creds)

            tee_time = choose_tee_time(fetch_tee_times(context, target))
            logger.info(
                f"Selected {tee_time.start:%Y-%m-%d %I:%M %p} ({tee_time.ui_label})"
            )

            set_date(page, target)
            select_slot(page, tee_time, target)
            choose_players_and_book(page)
            fill_payment_details(page, creds)

            if settings.submit_payment:
                submit_payment(page)
            else:
                logger.warning(
                    "TEE_TIME_SUBMIT_PAYMENT is off — stopping before Submit (dry run)"
                )
                _save_screenshot(page, settings.artifacts_dir, f"dryrun-{run_id}")
            return tee_time
        except Exception:
            _save_screenshot(page, settings.artifacts_dir, f"failure-{run_id}")
            raise
        finally:
            context.close()
            browser.close()


def _save_screenshot(page: Page, artifacts_dir: Path, name: str) -> None:
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = artifacts_dir / (re.sub(r"[^A-Za-z0-9_-]+", "_", name) + ".png")
        page.screenshot(path=str(path), full_page=True)
        logger.info(f"Saved screenshot to {path}")
    except Exception as e:  # never mask the original error
        logger.warning(f"Could not save screenshot: {e}")
