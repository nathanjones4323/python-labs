from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from tee_time_booker.courses import SATICOY, SOULE_PARK
from tee_time_booker.dates import (
    next_release,
    nth_upcoming_weekday,
    release_day,
    target_date,
)

LA = ZoneInfo("America/Los_Angeles")

SATURDAY = 5


def test_third_saturday_from_a_weekday():
    # Wed 2026-09-16 -> Saturdays are 9/19, 9/26, 10/3
    assert nth_upcoming_weekday(date(2026, 9, 16), SATURDAY, 3) == date(2026, 10, 3)


def test_today_counts_when_it_is_the_target_weekday():
    # Sat 2026-09-19 is itself the 1st upcoming Saturday
    assert nth_upcoming_weekday(date(2026, 9, 19), SATURDAY, 1) == date(2026, 9, 19)
    assert nth_upcoming_weekday(date(2026, 9, 19), SATURDAY, 3) == date(2026, 10, 3)


def test_sunday_rolls_to_next_week():
    # Sun 2026-09-20 -> next Saturday is 9/26
    assert nth_upcoming_weekday(date(2026, 9, 20), SATURDAY, 1) == date(2026, 9, 26)


@pytest.mark.parametrize("n", [0, -1])
def test_rejects_non_positive_n(n):
    with pytest.raises(ValueError):
        nth_upcoming_weekday(date(2026, 9, 16), SATURDAY, n)


def test_target_date_uses_course_local_day():
    # 20:30 UTC Sat 2026-09-19 is 13:30 PDT Saturday in Los Angeles.
    now = datetime(2026, 9, 19, 20, 30, tzinfo=ZoneInfo("UTC"))
    prefs = SOULE_PARK.preferences
    assert prefs.target_weekday == SATURDAY and prefs.weeks_ahead == 3
    assert target_date(SOULE_PARK, now) == date(2026, 10, 3)


def test_late_evening_run_belongs_to_the_coming_release():
    # 06:30 UTC Sun 2026-09-20 is 23:30 PDT Saturday: waiting on Sunday's
    # midnight release, which opens Sat 2026-10-10 (20 days out).
    now = datetime(2026, 9, 20, 6, 30, tzinfo=ZoneInfo("UTC"))
    assert release_day(SOULE_PARK, now) == date(2026, 9, 20)
    assert target_date(SOULE_PARK, now) == date(2026, 10, 10)
    assert next_release(SOULE_PARK, now) == datetime(2026, 9, 20, 0, 0, tzinfo=LA)


def test_run_after_midnight_needs_no_wait():
    now = datetime(2026, 9, 20, 0, 5, tzinfo=LA)
    assert release_day(SOULE_PARK, now) == date(2026, 9, 20)
    assert next_release(SOULE_PARK, now) is None
    assert target_date(SOULE_PARK, now) == date(2026, 10, 10)


def test_daytime_run_needs_no_wait():
    assert next_release(SOULE_PARK, datetime(2026, 9, 16, 12, 0, tzinfo=LA)) is None


def test_course_without_a_release_never_waits_or_rolls_over():
    # GolfNow's availability is rolling, so a late-evening run still books from
    # today rather than from the day about to begin.
    late = datetime(2026, 9, 19, 23, 30, tzinfo=LA)
    assert SATICOY.release is None
    assert release_day(SATICOY, late) == date(2026, 9, 19)
    assert next_release(SATICOY, late) is None
    assert target_date(SATICOY, late) == date(2026, 9, 19)
