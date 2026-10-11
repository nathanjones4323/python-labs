"""When a restaurant will start taking a reservation for a given date.

A booking window is "you may book N days/months ahead, from H:MM local". The
only arithmetic here is `target_date - N`, but the two units behave
differently and both edges matter:

* Months are calendar months, not 30 days. Nobu Malibu opens a date one month
  to the exact calendar date, so a 31 May dinner opens on 30 April (and a
  31 March dinner on 28 or 29 February — `relativedelta` clamps).
* The open time is wall-clock in the restaurant's own timezone, so a window
  that straddles a DST change still opens at 10:00 local on the day.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from date_planner.booking.channels import WindowState, WindowUnit

# Used when a restaurant states a window but not the hour it opens. Most
# places open the day's inventory with the morning; 09:00 is a guess, so
# `WindowResult.assumed_open_time` says it was one.
DEFAULT_OPEN_TIME = time(9, 0)


@dataclass(frozen=True)
class BookingWindow:
    """How far ahead a restaurant books, and from what local time."""

    amount: int
    unit: WindowUnit
    open_time: time | None = None


@dataclass(frozen=True)
class WindowResult:
    state: WindowState
    # Aware, in the restaurant's timezone. None when the state is unknown.
    opens_at: datetime | None
    # True when `DEFAULT_OPEN_TIME` stood in for a time nobody recorded, so
    # the caller can say "around 09:00" rather than "at 09:00".
    assumed_open_time: bool = False


def opens_at(
    window: BookingWindow, target_start_at: datetime, tz: ZoneInfo
) -> datetime:
    """The local moment bookings open for a date starting at `target_start_at`."""
    target_date = target_start_at.astimezone(tz).date()
    if window.unit is WindowUnit.MONTHS:
        open_date = target_date - relativedelta(months=window.amount)
    else:
        open_date = target_date - timedelta(days=window.amount)
    open_time = window.open_time or DEFAULT_OPEN_TIME
    # Combining a date and a wall-clock time under the zone (rather than
    # shifting a UTC instant) is what keeps "10:00 local" true across a DST
    # boundary.
    return datetime.combine(open_date, open_time).replace(tzinfo=tz)


def compute(
    window: BookingWindow | None,
    target_start_at: datetime,
    tz: ZoneInfo,
    now: datetime | None = None,
) -> WindowResult:
    """Whether `target_start_at` is bookable yet, and when it becomes so.

    `window` is None when the restaurant's window was never recorded or
    parsed; the result is then `unknown` and the caller should ask the user
    (or search the web) rather than guess.
    """
    if window is None:
        return WindowResult(state=WindowState.UNKNOWN, opens_at=None)
    moment = opens_at(window, target_start_at, tz)
    now = now or datetime.now(tz)
    state = WindowState.OPEN if now >= moment else WindowState.NOT_YET_OPEN
    return WindowResult(
        state=state,
        opens_at=moment,
        assumed_open_time=window.open_time is None,
    )
