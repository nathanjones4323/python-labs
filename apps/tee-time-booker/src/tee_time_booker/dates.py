"""Target-date selection."""

from datetime import date, datetime, timedelta

from tee_time_booker import config


def nth_upcoming_weekday(today: date, weekday: int, n: int) -> date:
    """Return the n-th upcoming occurrence of `weekday`, counting today if it matches.

    `weekday` follows `date.weekday()` (Monday=0 ... Sunday=6). n=1 is the next
    occurrence (or today), n=2 the one after that, and so on.
    """
    if n < 1:
        raise ValueError("n must be a positive integer")
    if not 0 <= weekday <= 6:
        raise ValueError("weekday must be in 0..6")
    days_until = (weekday - today.weekday()) % 7
    return today + timedelta(days=days_until + 7 * (n - 1))


def target_date(now: datetime | None = None) -> date:
    """The date to book, per the preferences in `config`, in course-local time."""
    now = now or datetime.now(config.COURSE_TZ)
    today = now.astimezone(config.COURSE_TZ).date()
    return nth_upcoming_weekday(today, config.TARGET_WEEKDAY, config.WEEKS_AHEAD)
