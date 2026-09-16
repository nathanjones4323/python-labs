from datetime import date, datetime, time

import pytest
from tee_time_booker.courses import SATICOY
from tee_time_booker.models import SearchCriteria
from tee_time_booker.platforms.golfnow import (
    allowed_players,
    parse_tee_times,
    search_payload,
)


def _criteria(holes: int = 9, players: int = 4) -> SearchCriteria:
    return SearchCriteria(
        target_date=date(2026, 10, 3),
        earliest=time(7, 0),
        latest=time(11, 0),
        players=players,
        holes=holes,
    )


def _rate(rate_id: int, holes: int = 9, player_rule: str = "Any") -> dict:
    """A rate trimmed down from a real GolfNow search response."""
    return {
        "teeTimeRateId": rate_id,
        "holeCount": holes,
        "playerRule": player_rule,
        "rateName": f"{holes} HOLES WALKING",
        "detailUrl": f"/tee-times/facility/1226/tee-time/{rate_id}",
        "singlePlayerPrice": {
            "greensFees": {"currencyCode": "USD", "value": 20.75},
            "transactionFeesTotal": {"currencyCode": "USD", "value": 2.39},
        },
    }


def _response(*rows: dict) -> dict:
    return {"ttResults": {"teeTimes": list(rows)}, "total": len(rows)}


def _row(stamp: str, *rates: dict) -> dict:
    return {
        "time": {"date": stamp, "formatted": stamp[11:16]},
        "teeTimeRates": list(rates),
    }


def test_parse_keeps_local_wall_time_and_prices():
    payload = _response(_row("2026-10-03T07:24:00+00:00", _rate(-890130711)))
    (tt,) = parse_tee_times(payload, SATICOY, _criteria())
    assert tt.platform == "golfnow"
    assert tt.course_name == SATICOY.name
    # GolfNow stamps course-local times with a +00:00 offset; 7:24 stays 7:24.
    assert tt.start == datetime(2026, 10, 3, 7, 24)
    assert tt.green_fee == 20.75
    assert tt.service_fee == 2.39
    assert tt.ref == "-890130711"
    assert tt.holes == 9


def test_parse_drops_rates_that_dont_match_holes_or_group_size():
    payload = _response(
        _row(
            "2026-10-03T08:00:00+00:00",
            _rate(1, holes=18),  # wrong hole count
            _rate(2, player_rule="OneTwo"),  # can't seat four
            _rate(3),  # keeper
        )
    )
    kept = parse_tee_times(payload, SATICOY, _criteria(holes=9, players=4))
    assert [t.ref for t in kept] == ["3"]


def test_parse_splits_a_tee_time_into_one_entry_per_bookable_rate():
    payload = _response(_row("2026-10-03T08:00:00+00:00", _rate(1), _rate(2)))
    assert len(parse_tee_times(payload, SATICOY, _criteria())) == 2


def test_parse_skips_rows_without_a_usable_time():
    payload = _response(
        {"time": {"date": "not a time"}, "teeTimeRates": [_rate(1)]},
        {"teeTimeRates": [_rate(2)]},
        _row("2026-10-03T09:00:00+00:00", _rate(3)),
    )
    assert [t.ref for t in parse_tee_times(payload, SATICOY, _criteria())] == ["3"]


def test_parse_returns_nothing_for_a_day_golfnow_hasnt_opened():
    payload = {
        "ttResults": {"teeTimes": []},
        "ttException": {"errorType": "HasNoTimes"},
    }
    assert parse_tee_times(payload, SATICOY, _criteria()) == []


def test_parse_rejects_a_response_that_isnt_a_search_result():
    with pytest.raises(ValueError):
        parse_tee_times({"oops": True}, SATICOY, _criteria())


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ("Any", {1, 2, 3, 4}),
        ("One", {1}),
        ("OneTwo", {1, 2}),
        ("TwoThreeFour", {2, 3, 4}),
        ("Four", {4}),
        ("", {1, 2, 3, 4}),
        (None, {1, 2, 3, 4}),
    ],
)
def test_player_rules_map_to_group_sizes(rule, expected):
    assert allowed_players(rule) == expected


def test_search_payload_identifies_the_facility_and_day():
    payload = search_payload(SATICOY, _criteria())
    assert payload["date"] == "Oct 03 2026"
    assert payload["facilityId"] == SATICOY.facility_id
    assert (payload["latitude"], payload["longitude"]) == (
        SATICOY.latitude,
        SATICOY.longitude,
    )
    # Filtering happens on the results, so the request stays wide open.
    assert (payload["timeMin"], payload["timeMax"]) == (0, 48)
    assert payload["players"] == 0
