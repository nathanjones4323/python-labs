"""Drives foreUP's booking UI for Soule Park with Playwright.

Flow (mirrors what a person does on the site):
  1. if we started before the midnight release, wait until just before it
  2. open the booking page and pick the booking class
  3. log in
  4. fetch the day's slots from the API using the browser's logged-in session
     (polling while the release is still a few seconds out) and choose one
     per our preferences
  5. show the date, click the slot (foreUP holds it for 5 minutes), choose
     holes (if asked) and players, "Book Time", confirm
  6. fill the card (Basis Theory hosted iframe) and postal code
  7. optionally click Submit

Synchronisation strategy: every UI step here triggers a specific foreUP API
call, so we wait on *that response* (``page.expect_response``) rather than on
DOM state or network idleness. The response also tells us whether the step
actually succeeded (e.g. whether the slot hold was granted), which the DOM
alone can't.

Locator strategy: foreUP's modals carry ``aria-hidden="true"`` even while
shown (a Bootstrap 3 quirk), so role-based locators can't see inside them.
Inside modals we anchor on the ids and ``data-*`` attributes foreUP's own JS
binds to; outside them we use role/label locators.
"""

import random
import re
import time
import urllib.parse
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from loguru import logger
from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    Response,
    expect,
    sync_playwright,
)
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tee_time_booker import config, dates
from tee_time_booker.config import Credentials, Settings
from tee_time_booker.tee_times import (
    TeeTime,
    api_query_params,
    choose_tee_time,
    parse_tee_times,
)

# How long to wait for any single element / response before giving up.
STEP_TIMEOUT_MS = 15_000
# After the midnight release, keep polling the API this long for slots to appear.
RELEASE_POLL_SECONDS = 15 * 60
RELEASE_POLL_INTERVAL_SECONDS = 3.0
# How many different slots to try holding before giving up.
MAX_HOLD_ATTEMPTS = 4
# Start the browser and log in this long before the release so the session is
# fresh but ready the moment slots appear.
RELEASE_LEAD_SECONDS = 45

# --- foreUP API paths the UI calls, used to wait on the right response ----------
LOGIN_API = "/api/booking/users/login"
TIMES_API = "/api/booking/times"
HOLD_API = "/api/booking/pending_reservation"
VALIDATE_API = "/api/booking/users/reservations"
PURCHASE_WINDOW_API = "/booking/credit_card_window/"
PROCESS_CARD_API = "/process_credit_card/"

# --- Selectors (inside modals; see module docstring) -----------------------------
LOGIN_MODAL = "#login"
LOGIN_EMAIL = "#login_email"
LOGIN_PASSWORD = "#login_password"
LOGIN_SUBMIT = "#login button.login"
DATE_FIELD = "#date-field"
SLOT_TILE = ".time-tile"
SLOT_TIME_LABEL = ".booking-start-time-label"
SLOT_SIDE_LABEL = ".js-booking-side-name"
BOOKING_MODAL = "#book_time"
BOOKING_MODAL_ERROR = "#book_time .js-booking-modal-error"
HOLES_BUTTON = f"#book_time [data-field='holes'] a[data-value='{config.HOLES}']"
PLAYERS_BUTTON = f"#book_time [data-field='players'] a[data-value='{config.PLAYERS}']"
BOOK_TIME_BUTTON = "#book_time .modal-footer button.js-book-button"
CONFIRM_BOOKING_BUTTON = "#payment_selection .modal-footer button.continue"
# Payment is an OdinPay form built on Basis Theory hosted elements: one iframe
# holds number / expiry / CVC; postal code is a plain input. No name field.
# The autocomplete attributes are what browsers key card autofill on, which
# makes them the most stable hooks in the vendor iframe.
CARD_IFRAME = "#payment_selection #cardWithPostalCode iframe"
CARD_NUMBER_INPUT = "input[autocomplete='cc-number']"
CARD_EXP_INPUT = "input[autocomplete='cc-exp']"
CARD_CVC_INPUT = "input[autocomplete='cc-csc']"
POSTAL_INPUT = "#payment_selection #postalCode input"
SUBMIT_PAYMENT_BUTTON = "#payment_selection button#submit"
# Site-wide toast foreUP uses for errors (App.ventErrorMessage -> bootstrapGrowl).
ERROR_TOAST = ".bootstrap-growl.alert-danger"


def _user_agent(browser_version: str) -> str:
    """A normal desktop Chrome UA matching the launched Chromium's major version.

    foreUP returns 403 to the default headless UA ("HeadlessChrome/...").
    """
    major = browser_version.split(".")[0]
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def _pause(low: float = 0.3, high: float = 0.9) -> None:
    """Small human-ish delay between UI steps. Not used for synchronisation."""
    time.sleep(random.uniform(low, high))


def _api(path: str, method: str = "GET") -> Callable[[Response], bool]:
    """Predicate for ``expect_response``: a foreUP call to `path` with `method`."""
    return lambda r: path in r.url and r.request.method == method


def _json(response: Response) -> dict:
    """The response's JSON object, or {} if it isn't one (e.g. an HTML error page)."""
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _exact(text: str) -> re.Pattern[str]:
    """Match an element whose whole (trimmed) text is `text`."""
    return re.compile(rf"^\s*{re.escape(text)}\s*$", re.IGNORECASE)


def booking_page_url(target: date) -> str:
    params = urllib.parse.urlencode(api_query_params(target), doseq=True)
    return f"{config.BOOKING_URL}#/teetimes?{params}"


# --- Steps --------------------------------------------------------------------


def open_booking_page(page: Page, target: date) -> None:
    page.goto(booking_page_url(target), wait_until="domcontentloaded")
    page.get_by_role("button", name=config.BOOKING_CLASS_NAME, exact=True).click()
    _pause()


def login(page: Page, creds: Credentials) -> None:
    page.get_by_role("link", name="Log In").click()
    page.locator(LOGIN_EMAIL).fill(creds.email)
    _pause()
    page.locator(LOGIN_PASSWORD).fill(creds.password)
    _pause()
    with page.expect_response(_api(LOGIN_API, "POST")) as info:
        page.locator(LOGIN_SUBMIT).click()
    response = info.value
    if not response.ok:
        raise RuntimeError(f"Login failed ({response.status}): {response.text()[:300]}")
    page.locator(LOGIN_MODAL).wait_for(state="hidden")
    logger.info("Logged in")
    _pause()


def wait_for_release(lead_seconds: float = RELEASE_LEAD_SECONDS) -> None:
    """Sleep until `lead_seconds` before the tee sheet we're booking opens."""
    release_at = dates.next_release()
    if release_at is None:
        return
    delay = (release_at - datetime.now(config.COURSE_TZ)).total_seconds() - lead_seconds
    if delay <= 0:
        return
    logger.info(
        f"Waiting {delay / 60:.1f} min for the {release_at:%Y-%m-%d %H:%M} release"
    )
    time.sleep(delay)


def fetch_tee_times(context: BrowserContext, target: date) -> list[TeeTime]:
    """Call the slots API via the browser context so it carries the login cookies."""
    response = context.request.get(
        config.TEE_TIMES_API_URL, params=api_query_params(target)
    )
    if not response.ok:
        raise RuntimeError(
            f"Tee time API returned {response.status}: {response.text()[:500]}"
        )
    return parse_tee_times(response.json())


def fetch_tee_times_when_released(
    context: BrowserContext, target: date, poll_seconds: float = RELEASE_POLL_SECONDS
) -> list[TeeTime]:
    """Fetch slots for `target`, polling while the day hasn't been released yet.

    The API returns an empty list for a day outside the booking window, so an
    empty result right after midnight most likely means the release is a few
    seconds late rather than that the day is sold out.
    """
    logger.info(f"Fetching tee times for {target:%A %Y-%m-%d}")
    deadline = time.monotonic() + poll_seconds
    attempt = 0
    while True:
        attempt += 1
        tee_times = fetch_tee_times(context, target)
        if tee_times:
            logger.info(f"API returned {len(tee_times)} tee times (attempt {attempt})")
            return tee_times
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"No tee times available on {target} after polling for "
                f"{poll_seconds / 60:.0f} min"
            )
        if attempt == 1:
            logger.info("No tee times yet; polling until they appear")
        time.sleep(RELEASE_POLL_INTERVAL_SECONDS)


def show_date(page: Page, target: date) -> None:
    """Put the slot list on `target` with a freshly loaded set of slots.

    Re-entering the date already shown doesn't make the UI re-fetch, so in
    that case step to the previous day first (the next one may be outside
    the booking window, which the date picker refuses).
    """
    field = page.locator(DATE_FIELD)
    if field.input_value() == target.strftime(config.FOREUP_DATE_FORMAT):
        _set_date(page, target - timedelta(days=1))
    _set_date(page, target)
    _pause()


def _set_date(page: Page, day: date) -> None:
    field = page.locator(DATE_FIELD)
    date_text = day.strftime(config.FOREUP_DATE_FORMAT)
    with page.expect_response(
        lambda r: TIMES_API in r.url and f"date={date_text}" in r.url and r.ok
    ):
        field.click()
        field.fill(date_text)
        field.press("Enter")


def slot_tile(page: Page, tee_time: TeeTime) -> Locator:
    """The tile for `tee_time` in the slot list.

    Tiles have no ARIA roles, so match on the rendered time label and, when
    known, the starting side ("Front"/"Back") since a clock time can appear
    once per side.
    """
    tile = page.locator(SLOT_TILE).filter(
        has=page.locator(SLOT_TIME_LABEL, has_text=_exact(tee_time.ui_label))
    )
    if tee_time.side:
        tile = tile.filter(
            has=page.locator(SLOT_SIDE_LABEL, has_text=_exact(tee_time.side))
        )
    return tile


def try_hold_slot(page: Page, tee_time: TeeTime) -> bool:
    """Click the slot's tile. True if foreUP granted the 5-minute hold.

    A slot someone else holds usually isn't in the list at all (the UI loads
    its own copy); if it's still rendered, the hold request is refused.
    """
    tile = slot_tile(page, tee_time)
    try:
        tile.wait_for(state="visible", timeout=3_000)
    except PlaywrightTimeoutError:
        logger.warning(f"Slot {tee_time.ui_label} is no longer listed")
        return False
    with page.expect_response(_api(HOLD_API, "POST")) as info:
        tile.click()
    response = info.value
    body = _json(response)
    if response.ok and body.get("success"):
        page.locator(BOOKING_MODAL).wait_for(state="visible")
        logger.info(f"Holding slot {tee_time.ui_label} ({body.get('reservation_id')})")
        return True
    logger.warning(
        f"Hold for {tee_time.ui_label} refused ({response.status}): "
        f"{response.text()[:300]}"
    )
    return False


def hold_slot(
    page: Page, context: BrowserContext, target: date, tee_times: list[TeeTime]
) -> TeeTime:
    """Choose the best slot and hold it; if the hold is refused, move to the next.

    A hold is refused when someone else grabbed the slot between our API read
    and the click, or when the account already holds a pending reservation.
    """
    tried: set[datetime] = set()
    for attempt in range(1, MAX_HOLD_ATTEMPTS + 1):
        tee_time = choose_tee_time(tee_times, exclude=tried)
        logger.info(
            f"Attempt {attempt}: {tee_time.start:%Y-%m-%d %I:%M %p} "
            f"{tee_time.side or ''} ${tee_time.green_fee}"
        )
        if try_hold_slot(page, tee_time):
            _pause()
            return tee_time
        tried.add(tee_time.start)
        _pause(1.5, 3.0)
        tee_times = fetch_tee_times(context, target)
        show_date(page, target)
    raise RuntimeError(
        f"Could not hold any slot on {target} in {MAX_HOLD_ATTEMPTS} attempts"
    )


def book_and_confirm(page: Page) -> None:
    """Fill the Book Time modal, submit it, confirm, and land on the payment form."""
    # Slots offered as "9 or 18" show a holes picker; 18-only slots don't. The
    # modal is rendered in one go, so if the picker isn't there now it won't be.
    holes = page.locator(HOLES_BUTTON)
    if holes.count():
        _select_option(holes)
    _select_option(page.locator(PLAYERS_BUTTON))

    with page.expect_response(_api(VALIDATE_API, "POST")) as info:
        page.locator(BOOK_TIME_BUTTON).click()
    validation = info.value
    if not (validation.ok and _json(validation).get("valid")):
        error = page.locator(BOOKING_MODAL_ERROR).inner_text().strip()
        detail = error or validation.text()[:300]
        raise RuntimeError(f"Booking rejected ({validation.status}): {detail}")
    _pause()

    with page.expect_response(_api(PURCHASE_WINDOW_API, "POST")) as info:
        page.locator(CONFIRM_BOOKING_BUTTON).click()
    if not info.value.ok:
        raise RuntimeError(f"Payment form failed to load ({info.value.status})")
    logger.info("Reached the payment form")
    _pause()


def _select_option(button: Locator) -> None:
    """Click one of foreUP's button-group options and confirm it took."""
    button.click()
    expect(button).to_have_class(re.compile(r"\bactive\b"))
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
        _pause(0.2, 0.5)
    logger.info("Filled payment details")


def submit_payment(page: Page, artifacts_dir: Path, run_id: str) -> None:
    """Click Submit and wait for foreUP to finish charging the card.

    Submit tokenises the card with Basis Theory, then foreUP posts the token
    to /booking/hydra_tokenize_card and finally to /process_credit_card/...,
    whose response is JavaScript the page evaluates to show the confirmation.
    We wait on that last call, keep its body for the record, then watch for
    either the confirmation route or an error toast.
    """
    with page.expect_response(_api(PROCESS_CARD_API, "POST"), timeout=60_000) as info:
        page.locator(SUBMIT_PAYMENT_BUTTON).click()
    response = info.value
    _save_text(artifacts_dir, f"payment-response-{run_id}.js", response.text())
    if not response.ok:
        raise RuntimeError(f"Payment processing failed ({response.status})")

    # Declines etc. surface as a bootstrapGrowl toast, success as a route change.
    toast = page.locator(ERROR_TOAST)
    confirmation = re.compile(r"#/confirmation/")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if toast.count():
            raise RuntimeError(f"Payment error: {toast.first.inner_text().strip()}")
        if confirmation.search(page.url):
            logger.info(f"Payment accepted; confirmation at {page.url}")
            return
        page.wait_for_timeout(500)
    logger.warning(
        "Payment submitted but no confirmation page or error seen within 30 s; "
        "check the artifacts and your foreUP account"
    )


# --- Orchestration --------------------------------------------------------------


def book_tee_time(
    target: date, creds: Credentials, settings: Settings, run_id: str
) -> TeeTime:
    """Run the full flow. Returns the booked slot; raises on any failure.

    On failure a screenshot and the page HTML are written to
    settings.artifacts_dir for debugging.
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
            # A testing override points at an already-open day; don't wait.
            if settings.target_date_override is None:
                wait_for_release()
            open_booking_page(page, target)
            login(page, creds)
            tee_times = fetch_tee_times_when_released(context, target)

            show_date(page, target)
            tee_time = hold_slot(page, context, target, tee_times)
            book_and_confirm(page)
            fill_payment_details(page, creds)

            if settings.submit_payment:
                submit_payment(page, settings.artifacts_dir, run_id)
                _save_artifacts(page, settings.artifacts_dir, f"confirmation-{run_id}")
            else:
                logger.warning(
                    "TEE_TIME_SUBMIT_PAYMENT is off — stopping before Submit (dry run)"
                )
                _save_artifacts(page, settings.artifacts_dir, f"dryrun-{run_id}")
            return tee_time
        except Exception:
            _save_artifacts(page, settings.artifacts_dir, f"failure-{run_id}")
            raise
        finally:
            context.close()
            browser.close()


def _save_text(artifacts_dir: Path, name: str, text: str) -> None:
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / name).write_text(text)
    except Exception as e:  # never mask the original error
        logger.warning(f"Could not save {name}: {e}")


def _save_artifacts(page: Page, artifacts_dir: Path, name: str) -> None:
    """Screenshot plus page HTML; the HTML is what you need to fix a selector."""
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        stem = artifacts_dir / re.sub(r"[^A-Za-z0-9_-]+", "_", name)
        page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
        stem.with_suffix(".html").write_text(page.content())
        logger.info(f"Saved {stem}.png and .html")
    except Exception as e:  # never mask the original error
        logger.warning(f"Could not save artifacts: {e}")
