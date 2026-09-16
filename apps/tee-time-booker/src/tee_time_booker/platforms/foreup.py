"""Books a foreUP course (Soule Park) with Playwright.

Flow (mirrors what a person does on the site):
  1. open the booking page and pick the booking class
  2. log in
  3. fetch the day's slots from the API using the browser's logged-in session
  4. show the date, click the slot (foreUP holds it for 5 minutes)
  5. choose holes (if asked) and players, "Book Time", confirm
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

import re
import time
import urllib.parse
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from loguru import logger
from playwright.sync_api import Locator, Response, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tee_time_booker.browser import pause
from tee_time_booker.courses import ForeUpCourse
from tee_time_booker.models import BookingResult, SearchCriteria, TeeTime
from tee_time_booker.platforms.base import BookingPlatform

BOOKING_URL = "https://foreupsoftware.com/index.php/booking/index/{course_id}"
TEE_TIMES_API_URL = "https://foreupsoftware.com/index.php/api/booking/times"
# Date format foreUP uses in both the API query string and the UI date field.
DATE_FORMAT = "%m-%d-%Y"

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


def _option_button(field: str, value: int) -> str:
    """One of the Book Time modal's button-group options (holes, players)."""
    return f"#book_time [data-field='{field}'] a[data-value='{value}']"


def api_query_params(course: ForeUpCourse, criteria: SearchCriteria) -> dict[str, str]:
    return {
        "time": "all",
        "date": criteria.target_date.strftime(DATE_FORMAT),
        "holes": str(criteria.holes),
        "players": str(criteria.players),
        "booking_class": course.booking_class,
        "schedule_id": course.schedule_id,
        "schedule_ids[]": course.schedule_id,
        "specials_only": "0",
        "api_key": "no_limits",
    }


def booking_page_url(course: ForeUpCourse, criteria: SearchCriteria) -> str:
    params = urllib.parse.urlencode(api_query_params(course, criteria), doseq=True)
    return f"{BOOKING_URL.format(course_id=course.course_id)}#/teetimes?{params}"


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_tee_times(payload: Any, course_name: str) -> list[TeeTime]:
    """Turn the API's JSON list into TeeTimes. Unparseable rows are skipped."""
    if not isinstance(payload, list):
        raise ValueError(f"Unexpected tee time payload: {payload!r}")

    tee_times: list[TeeTime] = []
    for row in payload:
        if not isinstance(row, dict) or "time" not in row:
            continue
        try:
            start = datetime.strptime(str(row["time"]), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        tee_times.append(
            TeeTime(
                platform=ForeUpPlatform.name,
                course_name=course_name,
                start=start,
                holes=_to_int(row.get("holes")),
                players=_to_int(row.get("maximum_players_per_booking")),
                green_fee=_to_float(row.get("green_fee")),
                side=str(row["teesheet_side_name"])
                if row.get("teesheet_side_name")
                else None,
            )
        )
    return tee_times


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


class ForeUpPlatform(BookingPlatform):
    name = "foreup"
    course_type = ForeUpCourse
    required_card_fields = (
        "number",
        "expiration_month",
        "expiration_year",
        "cvc",
        "postal_code",
    )

    course: ForeUpCourse

    def start(self, criteria: SearchCriteria) -> None:
        page = self.page
        page.goto(
            booking_page_url(self.course, criteria), wait_until="domcontentloaded"
        )
        page.get_by_role(
            "button", name=self.course.booking_class_name, exact=True
        ).click()
        pause()
        self._login()

    def _login(self) -> None:
        page = self.page
        account = self.credentials.account
        page.get_by_role("link", name="Log In").click()
        page.locator(LOGIN_EMAIL).fill(account.email)
        pause()
        page.locator(LOGIN_PASSWORD).fill(account.password)
        pause()
        with page.expect_response(_api(LOGIN_API, "POST")) as info:
            page.locator(LOGIN_SUBMIT).click()
        response = info.value
        if not response.ok:
            raise RuntimeError(
                f"Login failed ({response.status}): {response.text()[:300]}"
            )
        page.locator(LOGIN_MODAL).wait_for(state="hidden")
        logger.info("Logged in")
        pause()

    def find_tee_times(self, criteria: SearchCriteria) -> list[TeeTime]:
        """Call the slots API via the browser context so it carries the login cookies.

        foreUP returns an empty list for a day outside its booking window, so an
        empty result is "not released yet" rather than "sold out".
        """
        response = self.session.context.request.get(
            TEE_TIMES_API_URL, params=api_query_params(self.course, criteria)
        )
        if not response.ok:
            raise RuntimeError(
                f"Tee time API returned {response.status}: {response.text()[:500]}"
            )
        return parse_tee_times(response.json(), self.course.name)

    def hold(self, tee_time: TeeTime, criteria: SearchCriteria) -> bool:
        """Click the slot's tile. True if foreUP granted the 5-minute hold.

        A slot someone else holds usually isn't in the list at all (the UI loads
        its own copy); if it's still rendered, the hold request is refused.
        """
        page = self.page
        self._show_date(criteria.target_date)
        tile = self._slot_tile(tee_time)
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
            logger.info(
                f"Holding slot {tee_time.ui_label} ({body.get('reservation_id')})"
            )
            pause()
            return True
        logger.warning(
            f"Hold for {tee_time.ui_label} refused ({response.status}): "
            f"{response.text()[:300]}"
        )
        return False

    def _show_date(self, target: date) -> None:
        """Put the slot list on `target` with a freshly loaded set of slots.

        Re-entering the date already shown doesn't make the UI re-fetch, so in
        that case step to the previous day first (the next one may be outside
        the booking window, which the date picker refuses).
        """
        field = self.page.locator(DATE_FIELD)
        if field.input_value() == target.strftime(DATE_FORMAT):
            self._set_date(target - timedelta(days=1))
        self._set_date(target)
        pause()

    def _set_date(self, day: date) -> None:
        page = self.page
        field = page.locator(DATE_FIELD)
        date_text = day.strftime(DATE_FORMAT)
        with page.expect_response(
            lambda r: TIMES_API in r.url and f"date={date_text}" in r.url and r.ok
        ):
            field.click()
            field.fill(date_text)
            field.press("Enter")

    def _slot_tile(self, tee_time: TeeTime) -> Locator:
        """The tile for `tee_time` in the slot list.

        Tiles have no ARIA roles, so match on the rendered time label and, when
        known, the starting side ("Front"/"Back") since a clock time can appear
        once per side.
        """
        page = self.page
        tile = page.locator(SLOT_TILE).filter(
            has=page.locator(SLOT_TIME_LABEL, has_text=_exact(tee_time.ui_label))
        )
        if tee_time.side:
            tile = tile.filter(
                has=page.locator(SLOT_SIDE_LABEL, has_text=_exact(tee_time.side))
            )
        return tile

    def complete_booking(
        self, tee_time: TeeTime, criteria: SearchCriteria, *, submit_payment: bool
    ) -> BookingResult:
        self._book_and_confirm(criteria)
        self._fill_payment_details()
        if not submit_payment:
            logger.warning(
                "Payment submission is off — stopping before Submit (dry run)"
            )
            self.session.save_artifacts("dryrun")
            return BookingResult(tee_time=tee_time, submitted=False)
        confirmation = self._submit_payment(tee_time)
        self.session.save_artifacts("confirmation")
        return BookingResult(
            tee_time=tee_time, submitted=True, confirmation=confirmation
        )

    def _book_and_confirm(self, criteria: SearchCriteria) -> None:
        """Fill the Book Time modal, submit it, confirm, land on the payment form."""
        page = self.page
        # Slots offered as "9 or 18" show a holes picker; 18-only slots don't.
        # The modal is rendered in one go, so if it isn't there now it won't be.
        holes = page.locator(_option_button("holes", criteria.holes))
        if holes.count():
            _select_option(holes)
        _select_option(page.locator(_option_button("players", criteria.players)))

        with page.expect_response(_api(VALIDATE_API, "POST")) as info:
            page.locator(BOOK_TIME_BUTTON).click()
        validation = info.value
        if not (validation.ok and _json(validation).get("valid")):
            error = page.locator(BOOKING_MODAL_ERROR).inner_text().strip()
            detail = error or validation.text()[:300]
            raise RuntimeError(f"Booking rejected ({validation.status}): {detail}")
        pause()

        with page.expect_response(_api(PURCHASE_WINDOW_API, "POST")) as info:
            page.locator(CONFIRM_BOOKING_BUTTON).click()
        if not info.value.ok:
            raise RuntimeError(f"Payment form failed to load ({info.value.status})")
        logger.info("Reached the payment form")
        pause()

    def _fill_payment_details(self) -> None:
        page = self.page
        card = page.frame_locator(CARD_IFRAME)
        details = self.card
        # Hosted inputs format as you type, so send keystrokes rather than fill().
        fields = [
            (card.locator(CARD_NUMBER_INPUT), details.number),
            (
                card.locator(CARD_EXP_INPUT),
                details.expiration_month + details.expiration_year,
            ),
            (card.locator(CARD_CVC_INPUT), details.cvc),
            (page.locator(POSTAL_INPUT), details.postal_code),
        ]
        for locator, value in fields:
            locator.click()
            locator.press_sequentially(value, delay=40)
            pause(0.2, 0.5)
        logger.info("Filled payment details")

    def _submit_payment(self, tee_time: TeeTime) -> str | None:
        """Click Submit and wait for foreUP to finish charging the card.

        Submit tokenises the card with Basis Theory, then foreUP posts the token
        to /booking/hydra_tokenize_card and finally to /process_credit_card/...,
        whose response is JavaScript the page evaluates to show the confirmation.
        We wait on that last call, keep its body for the record, then watch for
        either the confirmation route or an error toast.
        """
        page = self.page
        with page.expect_response(
            _api(PROCESS_CARD_API, "POST"), timeout=60_000
        ) as info:
            page.locator(SUBMIT_PAYMENT_BUTTON).click()
        response = info.value
        self.session.save_text("payment-response.js", response.text())
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
                return page.url
            page.wait_for_timeout(500)
        logger.warning(
            "Payment submitted but no confirmation page or error seen within 30 s; "
            "check the artifacts and your foreUP account"
        )
        return None


def _select_option(button: Locator) -> None:
    """Click one of foreUP's button-group options and confirm it took."""
    button.click()
    expect(button).to_have_class(re.compile(r"\bactive\b"))
    pause()
