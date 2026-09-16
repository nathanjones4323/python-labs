from datetime import date, datetime, time

import pytest
from tee_time_booker.tee_times import (
    TeeTime,
    api_query_params,
    choose_tee_time,
    parse_tee_times,
)


def _slot(hhmm: str, fee: float = 50.0) -> TeeTime:
    return TeeTime(
        start=datetime.strptime(f"2026-10-03 {hhmm}", "%Y-%m-%d %H:%M"),
        green_fee=fee,
        holes=18,
        max_players=4,
    )


def test_parse_tee_times_reads_api_rows_and_skips_junk():
    payload = [
        {
            "time": "2026-10-03 08:10",
            "green_fee": "52",
            "holes": 18,
            "maximum_players_per_booking": "4",
        },
        {"time": "not a time"},
        {"nope": 1},
        "garbage",
    ]
    (tt,) = parse_tee_times(payload)
    assert tt.start == datetime(2026, 10, 3, 8, 10)
    assert tt.green_fee == 52.0
    assert tt.holes == 18
    assert tt.max_players == 4


def test_parse_tee_times_rejects_non_list():
    with pytest.raises(ValueError):
        parse_tee_times({"error": "nope"})


def test_ui_label_matches_foreup_format():
    assert _slot("08:00").ui_label == "8:00am"
    assert _slot("13:30").ui_label == "1:30pm"
    assert _slot("12:00").ui_label == "12:00pm"
    assert _slot("10:20").ui_label == "10:20am"


def test_choose_earliest_in_window():
    slots = [_slot("07:00"), _slot("14:10"), _slot("09:30"), _slot("08:20")]
    chosen = choose_tee_time(slots, earliest=time(8, 0), latest=time(14, 0))
    assert chosen.start.time() == time(8, 20)


def test_choose_latest_in_window_when_not_preferring_earlier():
    slots = [_slot("07:00"), _slot("13:50"), _slot("09:30"), _slot("14:00")]
    chosen = choose_tee_time(
        slots, earliest=time(8, 0), latest=time(14, 0), prefer_earlier=False
    )
    assert chosen.start.time() == time(14, 0)


def test_window_is_inclusive():
    chosen = choose_tee_time([_slot("08:00")], earliest=time(8, 0), latest=time(14, 0))
    assert chosen.start.time() == time(8, 0)


def test_no_slots_in_window_raises():
    with pytest.raises(ValueError, match="No tee times available"):
        choose_tee_time([_slot("06:00"), _slot("15:00")], time(8, 0), time(14, 0))


def test_api_query_params_use_foreup_date_format():
    params = api_query_params(date(2026, 10, 3))
    assert params["date"] == "10-03-2026"
    assert params["players"] == "4"
    assert params["holes"] == "18"
    assert params["schedule_ids[]"] == params["schedule_id"]
