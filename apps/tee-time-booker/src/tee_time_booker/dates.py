"""Target-date selection and release timing.

Some courses open each new day's tee sheet at a fixed local time (foreUP's
midnight drop); a run may start shortly *before* that release so it's logged in
and ready the moment slots appear, and such a run belongs to the day about to
begin, not the one ending. Courses without a release schedule (GolfNow's
rolling window) simply book from today.
"""

import time as time_module
from datetime import date, datetime, timedelta

from loguru import logger

from tee_time_booker.courses import CourseConfig
from tee_time_booker.models import Preferences

# Refuse to wait longer than this for a release. The GitHub job is killed at
# 110 min (tee-time-booker.yml), which would skip logging the run entirely, so
# fail early with a clear message instead.
MAX_RELEASE_WAIT_SECONDS = 95 * 60


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


def _local(course: CourseConfig, now: datetime | None) -> datetime:
    now = now or datetime.now(course.timezone)
    return now.astimezone(course.timezone)


def release_day(course: CourseConfig, now: datetime | None = None) -> date:
    """The course-local day this run books from.

    From `ReleaseSchedule.pre_release_start` onward that's tomorrow, because
    the run is waiting for tomorrow's release.
    """
    local = _local(course, now)
    if course.release and local.time() >= course.release.pre_release_start:
        return local.date() + timedelta(days=1)
    return local.date()


def next_release(course: CourseConfig, now: datetime | None = None) -> datetime | None:
    """When the tee sheet this run is waiting on opens, or None if it's already open."""
    if course.release is None:
        return None
    local = _local(course, now)
    day = release_day(course, local)
    if day == local.date():
        return None
    return datetime.combine(day, course.release.opens_at, tzinfo=course.timezone)


def target_date(
    course: CourseConfig, prefs: Preferences, now: datetime | None = None
) -> date:
    """The date `prefs` asks for at `course`, in course-local time."""
    return nth_upcoming_weekday(
        release_day(course, now), prefs.target_weekday, prefs.weeks_ahead
    )


def last_bookable_day(course: CourseConfig, now: datetime | None = None) -> date | None:
    """The furthest day `course`'s site takes bookings for, if that's fixed.

    None when the course's window isn't known up front and the adapter has to
    ask the site (`BookingPlatform.last_bookable_day`).
    """
    if course.booking_window_days is None:
        return None
    return release_day(course, now) + timedelta(days=course.booking_window_days)


def check_target_date(
    course: CourseConfig,
    target: date,
    last: date | None = None,
    now: datetime | None = None,
) -> None:
    """Raise unless `target` is a day `course`'s site will take a booking for.

    `last` is the last bookable day; it defaults to the course's fixed window
    and is passed in when the adapter has measured it on the site. Sites
    return an empty sheet for a day outside their window, which looks exactly
    like "not released yet", so without this a run would poll for the full
    release wait before failing.
    """
    first = release_day(course, now)
    if last is None:
        last = last_bookable_day(course, now)
    if target < first:
        raise ValueError(
            f"{course.name} target date {target} is in the past (today is {first})"
        )
    if last is not None and target > last:
        raise ValueError(
            f"{course.name} target date {target} is beyond its booking window; "
            f"{last} ({(last - first).days} days ahead) is the last bookable day"
        )


def sleep_until_release(
    course: CourseConfig,
    lead_seconds: float,
    max_wait_seconds: float = MAX_RELEASE_WAIT_SECONDS,
    now: datetime | None = None,
) -> None:
    """Sleep until `lead_seconds` before the tee sheet we're booking opens.

    Raises if that's more than `max_wait_seconds` away (e.g. a manual run
    started early in the evening) rather than sleeping past the job timeout.
    """
    now = now or datetime.now(course.timezone)
    release_at = next_release(course, now)
    if release_at is None:
        return
    delay = (release_at - now).total_seconds() - lead_seconds
    if delay <= 0:
        return
    if delay > max_wait_seconds:
        raise RuntimeError(
            f"The {release_at:%Y-%m-%d %H:%M} release is {delay / 60:.0f} min away, "
            f"more than the {max_wait_seconds / 60:.0f} min this run may wait; "
            "start later or pass --date / TEE_TIME_TARGET_DATE"
        )
    logger.info(
        f"Waiting {delay / 60:.1f} min for the {release_at:%Y-%m-%d %H:%M} release"
    )
    time_module.sleep(delay)
