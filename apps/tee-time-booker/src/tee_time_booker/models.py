"""The vocabulary every booking platform speaks.

A search is a `SearchCriteria`, a slot is a `TeeTime`, the outcome of booking
one is a `BookingResult`. Adapters translate their site's own shapes into
these; nothing platform-specific belongs here.
"""

from dataclasses import dataclass
from datetime import date, datetime, time


@dataclass(frozen=True)
class SearchCriteria:
    """What we're looking for on one day at one course."""

    target_date: date
    # Acceptable tee-off window (course-local), inclusive on both ends.
    earliest: time
    latest: time
    players: int
    holes: int
    # Within the window, take the earliest slot. False takes the latest.
    prefer_earlier: bool = True


@dataclass(frozen=True)
class TeeTime:
    """One bookable slot, as any platform reports it."""

    platform: str
    course_name: str
    start: datetime  # naive, course-local
    holes: int | None = None
    players: int | None = None  # most players this slot can seat
    green_fee: float | None = None  # per player, before fees
    service_fee: float | None = None  # booking / transaction fees, when known
    # Which nine the round starts on ("Front" / "Back"), when the platform says.
    # A clock time can appear once per side, so this disambiguates the slot.
    side: str | None = None
    # Opaque handle the adapter needs to book this slot again later (a rate id,
    # a detail URL, ...). Meaningful only to the adapter that produced it.
    ref: str | None = None

    @property
    def ui_label(self) -> str:
        """Time as booking sites render it, e.g. '8:00am', '1:30pm'."""
        return self.start.strftime("%I:%M%p").lstrip("0").lower()


@dataclass(frozen=True)
class BookingResult:
    """What came of booking a slot."""

    tee_time: TeeTime
    # False when the run stopped before the final Submit (a dry run).
    submitted: bool
    # Confirmation page URL or reservation id, when the platform gives one.
    confirmation: str | None = None

    @property
    def status(self) -> str:
        """The run-log status for this outcome."""
        return "Success" if self.submitted else "Success (dry run)"
