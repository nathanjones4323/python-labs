"""Target-date selection and release timing.

foreUP opens each day's tee sheet at course-local midnight `BOOKING_WINDOW_DAYS`
days ahead. A run may start shortly *before* that midnight (so it's logged in
and ready the moment slots appear); such a run belongs to the day about to
begin, not the one ending.
"""

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


def _local(now: datetime | None) -> datetime:
    now = now or datetime.now(config.COURSE_TZ)
    return now.astimezone(config.COURSE_TZ)


def release_day(now: datetime | None = None) -> date:
    """The course-local day this run books for.

    Late in the evening (from `config.PRE_RELEASE_START` onward) that's
    tomorrow, because the run is waiting for tomorrow's midnight release.
    """
    local = _local(now)
    if local.time() >= config.PRE_RELEASE_START:
        return local.date() + timedelta(days=1)
    return local.date()


def next_release(now: datetime | None = None) -> datetime | None:
    """When the tee sheet this run is waiting on opens, or None if it's already open."""
    local = _local(now)
    day = release_day(local)
    if day == local.date():
        return None
    return datetime.combine(day, config.RELEASE_TIME, tzinfo=config.COURSE_TZ)


def target_date(now: datetime | None = None) -> date:
    """The date to book, per the preferences in `config`, in course-local time."""
    return nth_upcoming_weekday(
        release_day(now), config.TARGET_WEEKDAY, config.WEEKS_AHEAD
    )
