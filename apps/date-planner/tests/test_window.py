"""Booking-window arithmetic: the edges that bite are months and DST."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest
from date_planner.booking.channels import WindowState, WindowUnit
from date_planner.booking.window import (
    DEFAULT_OPEN_TIME,
    BookingWindow,
    compute,
)

LA = ZoneInfo("America/Los_Angeles")
NY = ZoneInfo("America/New_York")

MONTH_AT_TEN = BookingWindow(1, WindowUnit.MONTHS, time(10, 0))
THIRTY_DAYS = BookingWindow(30, WindowUnit.DAYS, time(9, 0))


def la(year: int, month: int, day: int, hour: int = 19) -> datetime:
    return datetime(year, month, day, hour, tzinfo=LA)


@pytest.mark.parametrize(
    ("window", "target", "expected"),
    [
        # Nobu: one month to the exact calendar date, 10:00 local.
        (MONTH_AT_TEN, la(2026, 10, 24), datetime(2026, 9, 24, 10, tzinfo=LA)),
        # Month-end clamps rather than overflowing into March.
        (MONTH_AT_TEN, la(2026, 3, 31), datetime(2026, 2, 28, 10, tzinfo=LA)),
        # ... and lands on the 29th in a leap year.
        (MONTH_AT_TEN, la(2028, 3, 31), datetime(2028, 2, 29, 10, tzinfo=LA)),
        # 31 May - 1 month is 30 April, not 1 May.
        (MONTH_AT_TEN, la(2026, 5, 31), datetime(2026, 4, 30, 10, tzinfo=LA)),
        # Days are days.
        (THIRTY_DAYS, la(2026, 10, 24), datetime(2026, 9, 24, 9, tzinfo=LA)),
        # A window that crosses the end of DST still opens at 10:00 local:
        # 8 December is PST, 8 November is PDT.
        (MONTH_AT_TEN, la(2026, 12, 8), datetime(2026, 11, 8, 10, tzinfo=LA)),
        # And one that crosses the start of DST.
        (MONTH_AT_TEN, la(2026, 4, 10), datetime(2026, 3, 10, 10, tzinfo=LA)),
        # A week-count window, stored as days.
        (
            BookingWindow(14, WindowUnit.DAYS, time(8, 30)),
            la(2026, 10, 24),
            datetime(2026, 10, 10, 8, 30, tzinfo=LA),
        ),
    ],
)
def test_opens_at(window: BookingWindow, target: datetime, expected: datetime) -> None:
    result = compute(window, target, LA, now=datetime(2026, 1, 1, tzinfo=LA))
    assert result.opens_at == expected
    assert result.opens_at is not None
    # Same wall-clock hour whichever side of a DST change it falls.
    assert result.opens_at.hour == (window.open_time or DEFAULT_OPEN_TIME).hour


def test_not_yet_open_before_the_window() -> None:
    result = compute(
        MONTH_AT_TEN, la(2026, 10, 24), LA, now=datetime(2026, 9, 24, 9, 59, tzinfo=LA)
    )
    assert result.state is WindowState.NOT_YET_OPEN
    assert result.opens_at == datetime(2026, 9, 24, 10, tzinfo=LA)


def test_open_on_the_minute() -> None:
    result = compute(
        MONTH_AT_TEN, la(2026, 10, 24), LA, now=datetime(2026, 9, 24, 10, tzinfo=LA)
    )
    assert result.state is WindowState.OPEN


def test_open_once_inside_the_window() -> None:
    result = compute(
        MONTH_AT_TEN, la(2026, 10, 24), LA, now=datetime(2026, 10, 20, tzinfo=LA)
    )
    assert result.state is WindowState.OPEN


def test_no_window_recorded_is_unknown() -> None:
    result = compute(None, la(2026, 10, 24), LA)
    assert result.state is WindowState.UNKNOWN
    assert result.opens_at is None


def test_missing_open_time_defaults_and_says_so() -> None:
    result = compute(
        BookingWindow(1, WindowUnit.MONTHS),
        la(2026, 10, 24),
        LA,
        now=datetime(2026, 1, 1, tzinfo=LA),
    )
    assert result.assumed_open_time is True
    assert result.opens_at == datetime(2026, 9, 24, 9, tzinfo=LA)


def test_target_is_read_in_the_restaurants_timezone() -> None:
    # 00:30 on the 25th in New York is still the evening of the 24th in LA,
    # so the window is the 24th's.
    target = datetime(2026, 10, 25, 0, 30, tzinfo=NY)
    result = compute(MONTH_AT_TEN, target, LA, now=datetime(2026, 1, 1, tzinfo=LA))
    assert result.opens_at == datetime(2026, 9, 24, 10, tzinfo=LA)
