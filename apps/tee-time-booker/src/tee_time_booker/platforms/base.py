"""The contract every booking site implements.

An adapter is the *mechanics* of one site: how to sign in, how to list a day's
slots, how to take one, how to pay. The policy around those steps — waiting for
a release, picking the best slot, retrying the next one when a hold is refused,
logging the run — is shared and lives in `tee_time_booker.runner`, so adding a
site means implementing these four methods and nothing else.

The four are deliberately small and side-effect-ordered:

    start(criteria)                    open the site, sign in
    find_tee_times(criteria)           what's bookable that day
    hold(tee_time, criteria)           take this slot, False if it's gone
    complete_booking(tee_time, ...)    pay for the held slot

`hold` is separate from `complete_booking` because slots disappear between
listing and clicking: the runner needs to try the next-best one without
starting over, and on sites that grant a timed hold (foreUP's 5 minutes) the
two steps are genuinely different states.
"""

from abc import ABC, abstractmethod
from datetime import date
from typing import ClassVar

from tee_time_booker.browser import BrowserSession
from tee_time_booker.config import Credentials, PaymentDetails
from tee_time_booker.courses import CourseConfig
from tee_time_booker.models import BookingResult, SearchCriteria, TeeTime


class PaymentOutcomeUnknownError(RuntimeError):
    """Submit was clicked but neither a confirmation nor an error showed up.

    The card may have been charged; the caller must not report this as either
    success or a clean failure.
    """


class BookingPlatform(ABC):
    """Drives one booking site for one course."""

    # Name used in `CourseConfig.platform` and the registry.
    name: ClassVar[str]
    # Course dataclass this adapter needs (e.g. ForeUpCourse), checked on init.
    course_type: ClassVar[type[CourseConfig]]
    # `PaymentDetails` fields this site's checkout asks for.
    required_payment_fields: ClassVar[tuple[str, ...]]
    # Start the browser and sign in this long before a release, so the session
    # is fresh but ready the moment slots appear.
    release_lead_seconds: ClassVar[float] = 45
    # CSS selector for the checkout inputs holding card or personal details.
    # Hidden before every screenshot so none of it lands in artifacts, which
    # are easily shared when debugging a selector.
    sensitive_fields: ClassVar[str] = ""

    def __init__(
        self,
        course: CourseConfig,
        credentials: Credentials,
        session: BrowserSession,
    ) -> None:
        if not isinstance(course, self.course_type):
            raise TypeError(
                f"{type(self).__name__} needs a {self.course_type.__name__}, "
                f"got {type(course).__name__} for course {course.key!r}"
            )
        self.course = course
        self.credentials = credentials
        self.session = session

    @property
    def page(self):
        return self.session.page

    @property
    def payment(self) -> PaymentDetails:
        """What to pay with, once a run has got as far as needing it."""
        if self.credentials.payment is None:
            raise RuntimeError(
                f"Booking {self.course.name} needs payment details: "
                + ", ".join(
                    PaymentDetails.ENV_VARS[f] for f in self.required_payment_fields
                )
            )
        return self.credentials.payment

    def save_artifacts(self, name: str) -> None:
        """Screenshot plus page HTML, with this site's sensitive inputs hidden."""
        self.session.save_artifacts(name, hide=self.sensitive_fields)

    @abstractmethod
    def start(self, criteria: SearchCriteria) -> None:
        """Open the booking site and sign in, ready to search."""

    def last_bookable_day(self) -> date | None:
        """The furthest day the site currently takes bookings for, asked of the site.

        Called once, signed in, before the first search. Sites with a fixed,
        known window leave this returning None and set
        `CourseConfig.booking_window_days` instead, which needs no session and
        so can be checked before a release wait.
        """
        return None

    @abstractmethod
    def find_tee_times(self, criteria: SearchCriteria) -> list[TeeTime]:
        """Every slot bookable on `criteria.target_date` for its holes/players.

        Returns them unfiltered by time of day — choosing is the runner's job —
        and an empty list when the day isn't open for booking yet.
        """

    @abstractmethod
    def hold(self, tee_time: TeeTime, criteria: SearchCriteria) -> bool:
        """Take `tee_time` and open its checkout.

        Returns False when the slot is gone (someone else got it between the
        search and the click), which is ordinary — the runner moves on to the
        next-best slot. Raises only when the site itself misbehaves.
        """

    @abstractmethod
    def complete_booking(
        self, tee_time: TeeTime, criteria: SearchCriteria, *, submit_payment: bool
    ) -> BookingResult:
        """Fill checkout for the held slot and, unless dry-running, pay.

        A dry run stops before entering card details and leaves a screenshot
        behind, so the flow can be exercised without being charged and without
        card data reaching the artifacts.

        Raises `PaymentOutcomeUnknownError` when payment was submitted but the
        site showed neither a confirmation nor an error: the run must not be
        logged as a success or a clean failure.
        """
