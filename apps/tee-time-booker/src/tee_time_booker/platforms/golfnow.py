"""Books a GolfNow facility with Playwright.

Flow (mirrors what a person does on the site):
  1. open the account page; GolfNow's login is a GolfID iframe (email, then
     password on a second step)
  2. ask GolfNow's own search API for the day's tee times, through the
     browser's session so member pricing and cookies apply
  3. open the chosen rate's detail page, pick the number of golfers and
     "Continue to Book", which lands on that rate's checkout
  4. fill the card, billing details and phone, tick the terms
  5. optionally click Reserve

Why the API and not the results grid: GolfNow renders the grid client-side and
restyles it often, while the search endpoint returns the tee time id, hole
count, player rule and prices we need to pick a slot and jump straight to its
checkout URL. The endpoint, the login iframe and the detail-page controls below
were confirmed against the live site; the checkout field ids are carried over
from the older Selenium booker and are the most likely thing to need repair —
a failed run leaves the page HTML in `artifacts/` for exactly that.

GolfNow quirks the code works around:

- Times come back as ``2026-09-19T07:24:00+00:00`` but are course-local wall
  times, not UTC, so the offset is dropped rather than converted.
- One tee time carries several *rates* (9 vs 18 holes, walking vs cart), each
  with its own id, price and ``playerRule`` saying which group sizes it takes.
  Each bookable rate becomes its own `TeeTime`.
- The player-count radios are visually hidden inputs behind Tailwind labels,
  so the label is what gets clicked; a greyed-out (disabled) radio means that
  group size is gone.
"""

import re
from datetime import datetime
from typing import Any

from loguru import logger
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect

from tee_time_booker.browser import pause
from tee_time_booker.courses import GolfNowCourse
from tee_time_booker.models import BookingResult, SearchCriteria, TeeTime
from tee_time_booker.platforms.base import BookingPlatform

BASE_URL = "https://www.golfnow.com"
ACCOUNT_URL = f"{BASE_URL}/tee-times/my-account"
SEARCH_API_URL = f"{BASE_URL}/api/tee-times/tee-time-search-results"
FACILITY_URL = f"{BASE_URL}/tee-times/facility/{{seo_name}}/search"
DETAIL_URL = f"{BASE_URL}/tee-times/facility/{{facility_id}}/tee-time/{{rate_id}}"
# Date format the search API's "date" field wants, e.g. "Oct 03 2026".
DATE_FORMAT = "%b %d %Y"
# GolfNow's holes filter: "1" = 9, "2" = 18, "3" = any. We ask for any and
# match `holeCount` ourselves, so a course selling both is handled the same way.
HOLES_ANY = "3"

# --- Login (GolfID iframe) --------------------------------------------------------
LOGIN_FRAME = "#golfid-oauth-frame"
LOGIN_EMAIL = "#username"
LOGIN_EMAIL_SUBMIT = "#checkEmail"
LOGIN_PASSWORD = "#password"
LOGIN_SUBMIT = "#login"
# GolfNow bounces anonymous visitors to this page.
LOGIN_REDIRECT = re.compile(r"/account/login-redirect")

# --- Tee time detail page ---------------------------------------------------------
PLAYER_RADIO = "input[name='playerSelect'][value='{players}']"
# The radio is hidden; its label is the clickable control.
PLAYER_LABEL = "label[for='{radio_id}']"
PLAYER_RADIO_IDS = {
    1: "one-player",
    2: "two-player",
    3: "three-player",
    4: "four-player",
}
CONTINUE_TO_BOOK = "#cont-book"
CHECKOUT_URL_PATTERN = "/checkout/players/{players}"
# Interstitial GolfNow sometimes shows over the detail page.
MARKETING_POPUP_DISMISS = "#aInterstitialRemindLater"

# --- Checkout ---------------------------------------------------------------------
CARD_NUMBER_INPUT = "#billingcard"
CARD_EXP_MONTH_SELECT = "#cc-exp-month"
CARD_EXP_YEAR_SELECT = "#cc-exp-year"
CARD_CVC_INPUT = "#cc-cvv"
CARD_NAME_INPUT = "#billingname"
BILLING_ADDRESS_INPUT = "#billingaddress"
BILLING_POSTAL_INPUT = "#billingpostalcode"
PHONE_INPUT = "#reservationphonenumber"
TERMS_CHECKBOX = "input[id^='agree-terms']"
RESERVE_BUTTON = "#reservation-button-top, #reservation-button-bottom"
CONFIRMATION_URL = re.compile(r"/(confirmation|reservation-confirmed)", re.IGNORECASE)
CHECKOUT_ERROR = ".alert-danger, .error-message, [class*='errorMessage']"

# How long to wait for the confirmation page after clicking Reserve.
CONFIRMATION_TIMEOUT_SECONDS = 60

# GolfNow spells a rate's allowed group sizes as a word run: "Any", "One",
# "OneTwo", "TwoThreeFour", ...
_PLAYER_WORDS = {"One": 1, "Two": 2, "Three": 3, "Four": 4}
ALL_PLAYER_COUNTS = frozenset(_PLAYER_WORDS.values())


def allowed_players(player_rule: Any) -> frozenset[int]:
    """Group sizes a rate accepts. Anything unrecognised is treated as "any"."""
    words = re.findall("|".join(_PLAYER_WORDS), str(player_rule or ""))
    return frozenset(_PLAYER_WORDS[w] for w in words) or ALL_PLAYER_COUNTS


def search_payload(course: GolfNowCourse, criteria: SearchCriteria) -> dict[str, Any]:
    """The body GolfNow's search endpoint expects for one facility on one day.

    The time window and hole count are left wide open here and applied to the
    results instead: GolfNow reinterprets (and sometimes ignores) those filters
    server-side, and we would rather see every slot than trust its filtering.
    """
    return {
        "pageSize": 1000,
        "teeTimeCount": 20,
        "pageNumber": 0,
        "date": criteria.target_date.strftime(DATE_FORMAT),
        "sortBy": "Date",
        "sortByRollup": "Date.MinDate",
        "sortDirection": 0,
        "hotDealsOnly": False,
        "golfPassPerksOnly": False,
        "bestDealsOnly": False,
        "promotedCampaignsOnly": False,
        "priceMin": 0,
        "priceMax": 10000,
        "players": 0,  # 0 = any group size
        "holes": HOLES_ANY,
        "timePeriod": "Any",
        "timeMin": 0,  # half-hours from midnight: 0 = 12am, 48 = midnight
        "timeMax": 48,
        "facilityType": "GolfCourse",
        "facilityId": course.facility_id,
        "facilityIds": [],
        "latitude": course.latitude,
        "longitude": course.longitude,
        "radius": 35,
        "searchType": "Facility",
        "view": "Grouping",
        "rateType": "all",
        "excludeFeaturedFacilities": False,
        "excludePrivateFacilities": False,
        "trackmanOnly": False,
        "teeTimeId": 0,
        "currentClientDate": datetime.now().isoformat(timespec="milliseconds"),
    }


def _money(price: Any, key: str) -> float | None:
    """One amount out of GolfNow's money objects, e.g. greensFees -> 20.75."""
    if not isinstance(price, dict):
        return None
    amount = price.get(key)
    if isinstance(amount, dict):
        amount = amount.get("value")
    try:
        return float(amount)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _start_time(row: Any) -> datetime | None:
    """A tee time's start, as course-local wall time.

    GolfNow stamps local times with a "+00:00" offset, so the offset is
    dropped rather than converted.
    """
    stamp = row.get("time", {}).get("date") if isinstance(row, dict) else None
    try:
        return datetime.fromisoformat(str(stamp)).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def parse_tee_times(
    payload: Any, course: GolfNowCourse, criteria: SearchCriteria
) -> list[TeeTime]:
    """Turn a search response into the rates bookable for these criteria.

    Rates that don't match the requested hole count, or that won't take the
    requested group size, are dropped — they aren't bookable for this run.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected search payload: {payload!r}")
    rows = payload.get("ttResults", {}).get("teeTimes")
    if rows is None:
        rows = payload.get("results")
    if not isinstance(rows, list):
        raise ValueError(f"Search response had no tee times: {payload!r}"[:300])

    tee_times: list[TeeTime] = []
    for row in rows:
        start = _start_time(row)
        if start is None:
            continue
        for rate in row.get("teeTimeRates") or []:
            if not isinstance(rate, dict):
                continue
            holes = rate.get("holeCount")
            if holes != criteria.holes:
                continue
            players = allowed_players(rate.get("playerRule"))
            if criteria.players not in players:
                continue
            rate_id = rate.get("teeTimeRateId")
            if rate_id is None:
                continue
            price = rate.get("singlePlayerPrice")
            tee_times.append(
                TeeTime(
                    platform=GolfNowPlatform.name,
                    course_name=course.name,
                    start=start,
                    holes=holes,
                    players=max(players),
                    green_fee=_money(price, "greensFees"),
                    service_fee=_money(price, "transactionFeesTotal"),
                    ref=str(rate_id),
                )
            )
    return tee_times


class GolfNowPlatform(BookingPlatform):
    name = "golfnow"
    course_type = GolfNowCourse
    required_card_fields = (
        "number",
        "expiration_month",
        "expiration_year",
        "cvc",
        "postal_code",
        "name",
        "billing_address",
        "phone",
    )

    course: GolfNowCourse

    def start(self, criteria: SearchCriteria) -> None:
        page = self.page
        page.goto(ACCOUNT_URL, wait_until="domcontentloaded")
        self._dismiss_marketing_popup()
        self._login()

    def _login(self) -> None:
        """Sign in through the GolfID iframe: email, "Continue", then password.

        GolfNow only shows the password field once it has accepted the email,
        so the two steps can't be filled in one go.
        """
        page = self.page
        account = self.credentials.account
        try:
            page.locator(LOGIN_FRAME).wait_for(state="attached", timeout=10_000)
        except PlaywrightTimeoutError:
            # No login iframe: GolfNow served the account page itself, so this
            # browser session is already signed in.
            logger.info("Already signed in to GolfNow")
            return
        frame = page.frame_locator(LOGIN_FRAME)
        email = frame.locator(LOGIN_EMAIL)
        email.wait_for(state="visible")
        email.fill(account.email)
        pause()
        frame.locator(LOGIN_EMAIL_SUBMIT).click()
        password = frame.locator(LOGIN_PASSWORD)
        password.wait_for(state="visible")
        password.fill(account.password)
        pause()
        frame.locator(LOGIN_SUBMIT).click()
        # The iframe goes away and GolfNow reloads the page as the signed-in user.
        page.wait_for_url(lambda url: not LOGIN_REDIRECT.search(url))
        page.locator(LOGIN_FRAME).wait_for(state="detached")
        logger.info("Logged in")
        pause()

    def _dismiss_marketing_popup(self) -> None:
        """Close GolfNow's interstitial if it's up. Absent far more often than not."""
        dismiss = self.page.locator(MARKETING_POPUP_DISMISS)
        try:
            dismiss.click(timeout=2_000)
            logger.info("Dismissed GolfNow's marketing popup")
        except PlaywrightTimeoutError:
            pass

    def find_tee_times(self, criteria: SearchCriteria) -> list[TeeTime]:
        """Ask GolfNow's search API for the day's rates, through the signed-in session.

        GolfNow answers with an empty list (and a "no tee times" note) for a day
        it hasn't opened yet, which the runner treats as "not released".
        """
        response = self.session.context.request.post(
            SEARCH_API_URL,
            data=search_payload(self.course, criteria),
            headers={
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": FACILITY_URL.format(seo_name=self.course.seo_name),
            },
            timeout=60_000,
        )
        if not response.ok:
            raise RuntimeError(
                f"GolfNow search returned {response.status}: {response.text()[:500]}"
            )
        return parse_tee_times(response.json(), self.course, criteria)

    def hold(self, tee_time: TeeTime, criteria: SearchCriteria) -> bool:
        """Open the rate's page, pick the group size and continue to its checkout.

        GolfNow doesn't reserve anything until payment, so "holding" is really
        "got as far as checkout" — which is still the step that fails when
        somebody else took the slot, and the answer the runner needs.
        """
        if criteria.players not in PLAYER_RADIO_IDS:
            raise ValueError(f"GolfNow books 1-4 golfers, not {criteria.players}")
        page = self.page
        page.goto(self._detail_url(tee_time), wait_until="domcontentloaded")
        self._dismiss_marketing_popup()

        radio = page.locator(PLAYER_RADIO.format(players=criteria.players))
        if not radio.count():
            logger.warning(
                f"Slot {tee_time.ui_label} no longer offers {criteria.players} golfers"
            )
            return False
        if radio.is_disabled():
            logger.warning(
                f"Slot {tee_time.ui_label} is full for {criteria.players} golfers"
            )
            return False
        page.locator(
            PLAYER_LABEL.format(radio_id=PLAYER_RADIO_IDS[criteria.players])
        ).click()
        expect(radio).to_be_checked()
        pause()

        checkout = CHECKOUT_URL_PATTERN.format(players=criteria.players)
        page.locator(CONTINUE_TO_BOOK).click()
        try:
            page.wait_for_url(lambda url: checkout in url)
        except PlaywrightTimeoutError:
            if LOGIN_REDIRECT.search(page.url):
                raise RuntimeError(
                    "GolfNow asked to sign in again at checkout"
                ) from None
            logger.warning(
                f"Slot {tee_time.ui_label} did not reach checkout (at {page.url})"
            )
            return False
        logger.info(f"At checkout for {tee_time.ui_label} ({criteria.players} golfers)")
        pause()
        return True

    def _detail_url(self, tee_time: TeeTime) -> str:
        return DETAIL_URL.format(
            facility_id=self.course.facility_id, rate_id=tee_time.ref
        )

    def complete_booking(
        self, tee_time: TeeTime, criteria: SearchCriteria, *, submit_payment: bool
    ) -> BookingResult:
        self._fill_payment_details()
        if not submit_payment:
            logger.warning(
                "Payment submission is off — stopping before Reserve (dry run)"
            )
            self.session.save_artifacts("dryrun")
            return BookingResult(tee_time=tee_time, submitted=False)
        confirmation = self._reserve()
        self.session.save_artifacts("confirmation")
        return BookingResult(
            tee_time=tee_time, submitted=True, confirmation=confirmation
        )

    def _fill_payment_details(self) -> None:
        """Fill whatever of the checkout form this account is asked for.

        A saved card or profile means some of these fields simply aren't on the
        page, so each is filled only when present rather than required.
        """
        card = self.card
        for selector, value in (
            (CARD_NUMBER_INPUT, card.number),
            (CARD_CVC_INPUT, card.cvc),
            (CARD_NAME_INPUT, card.name),
            (BILLING_ADDRESS_INPUT, card.billing_address),
            (BILLING_POSTAL_INPUT, card.postal_code),
            (PHONE_INPUT, card.phone),
        ):
            self._fill_if_present(selector, value)
        self._select_expiry(CARD_EXP_MONTH_SELECT, card.expiration_month)
        self._select_expiry(CARD_EXP_YEAR_SELECT, card.expiration_year)

        terms = self.page.locator(TERMS_CHECKBOX)
        if terms.count():
            terms.first.check()
        pause()
        logger.info("Filled payment details")

    def _fill_if_present(self, selector: str, value: str | None) -> None:
        field = self.page.locator(selector)
        if not value or not field.count():
            return
        field.first.fill("")
        field.first.press_sequentially(value, delay=40)
        pause(0.2, 0.5)

    def _select_expiry(self, selector: str, value: str) -> None:
        """Pick an expiry month/year, whichever spelling the dropdown uses.

        The year dropdown has shipped as both "26" and "2026"; the month one as
        both "3" and "03".
        """
        field = self.page.locator(selector)
        if not field.count():
            return
        candidates = [value, value.lstrip("0"), f"20{value}"]
        for candidate in dict.fromkeys(candidates):
            try:
                field.first.select_option(value=candidate)
                return
            except Exception:
                continue
        field.first.select_option(label=value)

    def _reserve(self) -> str | None:
        """Click Reserve and wait for GolfNow to confirm the booking."""
        page = self.page
        page.locator(RESERVE_BUTTON).first.click()
        error = page.locator(CHECKOUT_ERROR)
        for _ in range(CONFIRMATION_TIMEOUT_SECONDS * 2):
            if CONFIRMATION_URL.search(page.url):
                logger.info(f"Reservation confirmed at {page.url}")
                return page.url
            if error.count() and error.first.is_visible():
                raise RuntimeError(
                    f"GolfNow rejected the booking: {error.first.inner_text().strip()}"
                )
            page.wait_for_timeout(500)
        logger.warning(
            "Reserve clicked but no confirmation page or error seen within "
            f"{CONFIRMATION_TIMEOUT_SECONDS} s; check the artifacts and your "
            "GolfNow account"
        )
        return None
