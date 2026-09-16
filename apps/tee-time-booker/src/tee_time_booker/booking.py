"""Drives foreUP's booking UI for Soule Park with Playwright.

Flow (mirrors what a person does on the site):
  1. open the booking page and pick the public booking class
  2. log in
  3. fetch the day's slots from the API using the browser's logged-in session
     and choose one per our preferences
  4. set the date, click the slot, choose 4 players, "Book Time", confirm
  5. fill the card fields (each lives in its own iframe)
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
PLAYERS_ROW = "#book_time .js-booking-players a"
BOOK_TIME_BUTTON = "#book_time .modal-footer button.js-book-button"
CONFIRM_BOOKING_BUTTON = "#payment_selection .modal-footer button.continue"
CARD_IFRAMES = {
    "name": "#field-wrapper-name iframe",
    "number": "#field-wrapper-number iframe",
    "expiration": "#field-wrapper-expiration_date iframe",
    "cvc": "#field-wrapper-security_code iframe",
    "postal_code": "#field-wrapper-postal_code iframe",
}
SUBMIT_PAYMENT_BUTTON = "#btnSubmitMobile"


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
    _pause(0.5, 1.0)


def select_slot(page: Page, tee_time: TeeTime) -> None:
    label = tee_time.ui_label
    # Anchor on digit boundaries so "1:00pm" can't match "11:00pm".
    pattern = re.compile(rf"(?<!\d){re.escape(label)}(?!\w)", re.IGNORECASE)
    slot = page.locator(SLOT_TIME_LABEL, has_text=pattern).first
    slot.wait_for(state="visible")
    slot.click()
    logger.info(f"Clicked slot {label}")
    _pause()


def choose_players_and_book(page: Page) -> None:
    page.locator(PLAYERS_ROW).nth(config.PLAYERS - 1).click()
    _pause()
    page.click(BOOK_TIME_BUTTON)
    _pause()
    page.click(CONFIRM_BOOKING_BUTTON)
    _pause()


def fill_payment_details(page: Page, creds: Credentials) -> None:
    values = {
        "name": creds.card_name,
        "number": creds.card_number,
        "expiration": f"{creds.card_expiration_month}{creds.card_expiration_year}",
        "cvc": creds.card_cvc,
        "postal_code": creds.card_postal_code,
    }
    for field, value in values.items():
        frame = page.frame_locator(CARD_IFRAMES[field])
        frame.locator("input").first.fill(value)
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
            select_slot(page, tee_time)
            choose_players_and_book(page)
            fill_payment_details(page, creds)

            if settings.submit_payment:
                submit_payment(page)
            else:
                logger.warning(
                    "TEE_TIME_SUBMIT_PAYMENT is off — stopping before Submit (dry run)"
                )
            return tee_time
        except Exception:
            _save_screenshot(page, settings.artifacts_dir, run_id)
            raise
        finally:
            context.close()
            browser.close()


def _save_screenshot(page: Page, artifacts_dir: Path, run_id: str) -> None:
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", run_id)
        path = artifacts_dir / f"failure-{safe_id}.png"
        page.screenshot(path=str(path), full_page=True)
        logger.info(f"Saved failure screenshot to {path}")
    except Exception as e:  # never mask the original error
        logger.warning(f"Could not save failure screenshot: {e}")
