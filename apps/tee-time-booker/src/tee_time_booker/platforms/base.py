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
from typing import ClassVar

from tee_time_booker.browser import BrowserSession
from tee_time_booker.config import Credentials, PaymentCard
from tee_time_booker.courses import CourseConfig
from tee_time_booker.models import BookingResult, SearchCriteria, TeeTime


class BookingPlatform(ABC):
    """Drives one booking site for one course."""

    # Name used in `CourseConfig.platform` and the registry.
    name: ClassVar[str]
    # Course dataclass this adapter needs (e.g. ForeUpCourse), checked on init.
    course_type: ClassVar[type[CourseConfig]]
    # `PaymentCard` fields this site's checkout asks for.
    required_card_fields: ClassVar[tuple[str, ...]]
    # Start the browser and sign in this long before a release, so the session
    # is fresh but ready the moment slots appear.
    release_lead_seconds: ClassVar[float] = 45

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
    def card(self) -> PaymentCard:
        """The card to pay with, once a run has got as far as needing one."""
        if self.credentials.card is None:
            raise RuntimeError(
                f"Booking {self.course.name} needs card details: "
                + ", ".join(PaymentCard.ENV_VARS[f] for f in self.required_card_fields)
            )
        return self.credentials.card

    @abstractmethod
    def start(self, criteria: SearchCriteria) -> None:
        """Open the booking site and sign in, ready to search."""

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

        A dry run stops before the final Submit and leaves a screenshot behind,
        so the whole flow can be exercised without being charged.
        """
