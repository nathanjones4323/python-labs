from datetime import date, datetime, time

import pytest
from tee_time_booker.courses import SOULE_PARK
from tee_time_booker.models import SearchCriteria
from tee_time_booker.platforms.foreup import (
    api_query_params,
    booking_page_url,
    parse_tee_times,
)

CRITERIA = SearchCriteria(
    target_date=date(2026, 10, 3),
    earliest=time(8, 0),
    latest=time(14, 0),
    players=4,
    holes=18,
)


def test_parse_tee_times_reads_api_rows_and_skips_junk():
    payload = [
        {
            "time": "2026-10-03 08:10",
            "green_fee": "52",
            "holes": 18,
            "maximum_players_per_booking": "4",
            "teesheet_side_name": "Front",
        },
        {"time": "not a time"},
        {"nope": 1},
        "garbage",
    ]
    (tt,) = parse_tee_times(payload, SOULE_PARK.name)
    assert tt.platform == "foreup"
    assert tt.course_name == "Soule Park"
    assert tt.start == datetime(2026, 10, 3, 8, 10)
    assert tt.green_fee == 52.0
    assert tt.holes == 18
    assert tt.players == 4
    assert tt.side == "Front"


def test_parse_tee_times_rejects_non_list():
    with pytest.raises(ValueError):
        parse_tee_times({"error": "nope"}, SOULE_PARK.name)


def test_api_query_params_use_foreup_date_format_and_course_ids():
    params = api_query_params(SOULE_PARK, CRITERIA)
    assert params["date"] == "10-03-2026"
    assert params["players"] == "4"
    assert params["holes"] == "18"
    assert params["booking_class"] == SOULE_PARK.booking_class
    assert params["schedule_ids[]"] == params["schedule_id"] == SOULE_PARK.schedule_id


def test_booking_page_url_targets_the_courses_tee_sheet():
    url = booking_page_url(SOULE_PARK, CRITERIA)
    assert url.startswith(
        f"https://foreupsoftware.com/index.php/booking/index/{SOULE_PARK.course_id}"
    )
    assert "#/teetimes?" in url
    assert "date=10-03-2026" in url
