from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from tee_time_booker import config
from tee_time_booker.dates import nth_upcoming_weekday, target_date

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
    # 06:30 UTC Sun 2026-09-20 is still Sat 2026-09-19 (23:30 PDT) in Los Angeles.
    now = datetime(2026, 9, 20, 6, 30, tzinfo=ZoneInfo("UTC"))
    assert config.TARGET_WEEKDAY == SATURDAY and config.WEEKS_AHEAD == 3
    assert target_date(now) == date(2026, 10, 3)
