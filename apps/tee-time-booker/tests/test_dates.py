from dataclasses import replace
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from tee_time_booker.courses import SATICOY, SOULE_PARK
from tee_time_booker.dates import (
    check_target_date,
    last_bookable_day,
    next_release,
    nth_upcoming_weekday,
    release_day,
    sleep_until_release,
    target_date,
)
from tee_time_booker.models import Preferences

LA = ZoneInfo("America/Los_Angeles")

SATURDAY = 5

# The Soule Park plan's preferences: the 3rd upcoming Saturday.
THIRD_SATURDAY = Preferences(
    target_weekday=SATURDAY,
    weeks_ahead=3,
    earliest=time(8, 0),
    latest=time(14, 0),
    holes=18,
    players=4,
)
NEXT_SATURDAY = replace(THIRD_SATURDAY, weeks_ahead=1)


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
    assert target_date(SOULE_PARK, THIRD_SATURDAY, now) == date(2026, 10, 3)


def test_late_evening_run_belongs_to_the_coming_release():
    # 06:30 UTC Sun 2026-09-20 is 23:30 PDT Saturday: waiting on Sunday's
    # midnight release, which opens Sat 2026-10-10 (20 days out).
    now = datetime(2026, 9, 20, 6, 30, tzinfo=ZoneInfo("UTC"))
    assert release_day(SOULE_PARK, now) == date(2026, 9, 20)
    assert target_date(SOULE_PARK, THIRD_SATURDAY, now) == date(2026, 10, 10)
    assert next_release(SOULE_PARK, now) == datetime(2026, 9, 20, 0, 0, tzinfo=LA)


def test_run_after_midnight_needs_no_wait():
    now = datetime(2026, 9, 20, 0, 5, tzinfo=LA)
    assert release_day(SOULE_PARK, now) == date(2026, 9, 20)
    assert next_release(SOULE_PARK, now) is None
    assert target_date(SOULE_PARK, THIRD_SATURDAY, now) == date(2026, 10, 10)


def test_daytime_run_needs_no_wait():
    assert next_release(SOULE_PARK, datetime(2026, 9, 16, 12, 0, tzinfo=LA)) is None


def test_course_without_a_release_never_waits_or_rolls_over():
    # GolfNow's availability is rolling, so a late-evening run still books from
    # today rather than from the day about to begin.
    late = datetime(2026, 9, 19, 23, 30, tzinfo=LA)
    assert SATICOY.release is None
    assert release_day(SATICOY, late) == date(2026, 9, 19)
    assert next_release(SATICOY, late) is None
    assert target_date(SATICOY, NEXT_SATURDAY, late) == date(2026, 9, 19)


def test_release_too_far_away_fails_instead_of_sleeping_past_the_job_timeout():
    # 20:30 PDT Saturday: inside the pre-release window, but the midnight
    # release is still 3.5 h off.
    now = datetime(2026, 9, 19, 20, 30, tzinfo=LA)
    with pytest.raises(RuntimeError, match="209 min away, more than the 95 min"):
        sleep_until_release(SOULE_PARK, 45, max_wait_seconds=95 * 60, now=now)


def test_release_within_the_cap_sleeps_until_the_lead(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("tee_time_booker.dates.time_module.sleep", slept.append)
    now = datetime(2026, 9, 19, 23, 50, tzinfo=LA)
    sleep_until_release(SOULE_PARK, 60, now=now)
    assert slept == [pytest.approx(9 * 60)]


def test_no_release_pending_returns_without_sleeping(monkeypatch):
    monkeypatch.setattr(
        "tee_time_booker.dates.time_module.sleep",
        lambda _: pytest.fail("should not sleep"),
    )
    sleep_until_release(SOULE_PARK, 45, now=datetime(2026, 9, 16, 12, 0, tzinfo=LA))
    sleep_until_release(SATICOY, 45, now=datetime(2026, 9, 19, 23, 30, tzinfo=LA))


def test_target_inside_a_fixed_window_passes():
    now = datetime(2026, 9, 16, 12, 0, tzinfo=LA)
    assert last_bookable_day(SOULE_PARK, now) == date(2026, 10, 6)
    check_target_date(SOULE_PARK, date(2026, 10, 6), now=now)


def test_target_past_a_fixed_window_is_refused_up_front():
    now = datetime(2026, 9, 16, 12, 0, tzinfo=LA)
    with pytest.raises(ValueError, match="2026-10-06 \\(20 days ahead\\) is the last"):
        check_target_date(SOULE_PARK, date(2026, 10, 7), now=now)


def test_fixed_window_counts_from_the_coming_release():
    # 23:30 Saturday waits for Sunday's release, so the window runs from Sunday.
    now = datetime(2026, 9, 19, 23, 30, tzinfo=LA)
    assert last_bookable_day(SOULE_PARK, now) == date(2026, 10, 10)
    check_target_date(SOULE_PARK, date(2026, 10, 10), now=now)


def test_past_target_is_refused():
    now = datetime(2026, 9, 16, 12, 0, tzinfo=LA)
    with pytest.raises(ValueError, match="in the past"):
        check_target_date(SOULE_PARK, date(2026, 9, 15), now=now)


def test_course_without_a_fixed_window_defers_to_the_site():
    now = datetime(2026, 9, 17, 12, 0, tzinfo=LA)
    assert last_bookable_day(SATICOY, now) is None
    # Nothing known up front: any future day passes...
    check_target_date(SATICOY, date(2026, 10, 3), now=now)
    # ...until the adapter has measured the window.
    with pytest.raises(ValueError, match="2026-09-24 \\(7 days ahead\\) is the last"):
        check_target_date(SATICOY, date(2026, 10, 3), last=date(2026, 9, 24), now=now)
