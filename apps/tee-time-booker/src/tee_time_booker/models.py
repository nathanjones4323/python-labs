"""The vocabulary every booking platform speaks.

A search is a `SearchCriteria`, a slot is a `TeeTime`, the outcome of booking
one is a `BookingResult`; `Preferences` is a standing wish that turns into a
`SearchCriteria` once a date is chosen. Adapters translate their site's own
shapes into these; nothing platform-specific belongs here.
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


@dataclass(frozen=True, kw_only=True)
class Preferences:
    """Which day, which time window, how big a group — a user's standing wish.

    Stored on a booking plan in the database; `criteria()` pins it to a date.
    """

    # Book the Nth upcoming occurrence of `target_weekday` (Monday=0 ...
    # Sunday=6); "upcoming" includes today if today is that weekday.
    target_weekday: int
    weeks_ahead: int
    # Acceptable tee-off window (course-local), inclusive on both ends.
    earliest: time
    latest: time
    holes: int
    players: int
    # Within the window, take the earliest slot. False takes the latest.
    prefer_earlier: bool = True

    def criteria(self, target_date: date) -> SearchCriteria:
        """The search these preferences describe for `target_date`."""
        return SearchCriteria(
            target_date=target_date,
            earliest=self.earliest,
            latest=self.latest,
            players=self.players,
            holes=self.holes,
            prefer_earlier=self.prefer_earlier,
        )


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
