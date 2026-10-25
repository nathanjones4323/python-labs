from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from loguru import logger
from tee_time_booker.config import Account, Credentials
from tee_time_booker.courses import SATICOY
from tee_time_booker.models import SearchCriteria
from tee_time_booker.platforms import PaymentOutcomeUnknownError, golfnow
from tee_time_booker.platforms.golfnow import (
    WINDOW_PROBE_EMPTY_DAYS,
    WINDOW_PROBE_MAX_DAYS,
    GolfNowPlatform,
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


def test_parse_explains_an_open_day_where_nothing_fits():
    """The runner polls on an empty list; make clear when polling is pointless."""
    payload = _response(
        _row("2026-10-03T08:00:00+00:00", _rate(1, holes=18), _rate(2, holes=18)),
        _row("2026-10-03T09:00:00+00:00", _rate(3, player_rule="OneTwo")),
    )
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    try:
        assert parse_tee_times(payload, SATICOY, _criteria(holes=9, players=4)) == []
    finally:
        logger.remove(sink)
    (message,) = messages
    assert "listed 2 tee times" in message
    assert "2 rates wrong hole count" in message
    assert "1 won't take that group size" in message


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
    payload = search_payload(SATICOY, date(2026, 10, 3))
    assert payload["date"] == "Oct 03 2026"
    assert payload["facilityId"] == SATICOY.facility_id
    assert (payload["latitude"], payload["longitude"]) == (
        SATICOY.latitude,
        SATICOY.longitude,
    )
    # Filtering happens on the results, so the request stays wide open.
    assert (payload["timeMin"], payload["timeMax"]) == (0, 48)
    assert payload["players"] == 0


TODAY = date(2026, 9, 17)


def _probing_platform(monkeypatch, listed_offsets: set[int]):
    """A GolfNowPlatform whose search API has tee times `listed_offsets` days out.

    Returns it with the list of days it searches, in order.
    """
    platform = GolfNowPlatform(
        SATICOY, Credentials(account=Account("a@example.com", "pw")), session=None
    )
    searched: list[date] = []

    def fake_search(day: date) -> dict:
        searched.append(day)
        if (day - TODAY).days not in listed_offsets:
            return _response()
        return _response(_row(f"{day}T08:00:00+00:00", _rate(1)))

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.combine(TODAY, time(10, 0), tzinfo=tz)

    monkeypatch.setattr(platform, "_search", fake_search)
    monkeypatch.setattr(golfnow, "datetime", FrozenDateTime)
    return platform, searched


def test_window_ends_at_the_last_listed_day(monkeypatch):
    platform, searched = _probing_platform(monkeypatch, set(range(8)))
    assert platform.last_bookable_day() == date(2026, 9, 24)
    # Stops after a few empty days rather than scanning to the cap.
    assert searched[-1] == TODAY + timedelta(days=7 + WINDOW_PROBE_EMPTY_DAYS)


def test_window_survives_closed_days_in_the_middle(monkeypatch):
    listed = set(range(15)) - {3, 4}  # shut for two days mid-window
    platform, _ = _probing_platform(monkeypatch, listed)
    assert platform.last_bookable_day() == TODAY + timedelta(days=14)


def test_window_is_unknown_when_nothing_is_listed(monkeypatch):
    platform, searched = _probing_platform(monkeypatch, set())
    assert platform.last_bookable_day() is None
    assert len(searched) == WINDOW_PROBE_EMPTY_DAYS


def test_window_wider_than_the_probe_cap_is_left_open(monkeypatch):
    platform, searched = _probing_platform(monkeypatch, set(range(400)))
    assert platform.last_bookable_day() is None
    assert len(searched) == WINDOW_PROBE_MAX_DAYS + 1


# --- Checkout, against stand-in pages in a real browser ------------------------


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except sync_api.Error:
            pytest.skip("Chromium isn't installed (playwright install chromium)")
        yield browser
        browser.close()


@pytest.fixture
def platform(browser, monkeypatch):
    """A GolfNowPlatform on a blank page, with no waiting between steps."""
    page = browser.new_page()
    page.set_default_timeout(2_000)
    monkeypatch.setattr(golfnow, "pause", lambda *a, **k: None)
    monkeypatch.setattr(golfnow, "CONFIRMATION_TIMEOUT_SECONDS", 2)
    session = SimpleNamespace(page=page)
    yield GolfNowPlatform(
        SATICOY, Credentials(account=Account("a@example.com", "pw")), session
    )
    page.close()


OFFLINE_HTML = "<h1>You appear to be offline</h1><button>Retry</button>"


def _checkout_html(name: str = "Pat Golfer", address: str = "1 Main St") -> str:
    return f"""
        <form id="fmcheck">
          <input id="billingname" value="{name}">
          <input id="billingaddress" value="{address}">
          <input id="billingpostalcode" value="">
        </form>
    """


def test_billing_check_passes_with_a_name_and_address(platform):
    platform.page.set_content(_checkout_html())
    platform._check_billing_profile()


@pytest.mark.parametrize(
    ("name", "address", "missing"),
    [
        ("Pat Golfer", "", "street address"),
        ("", "1 Main St", "name"),
        (" ", " ", "name or street address"),
    ],
)
def test_billing_check_stops_without_a_name_or_address(
    platform, name, address, missing
):
    platform.page.set_content(_checkout_html(name, address))
    with pytest.raises(RuntimeError, match=f"no billing {missing},"):
        platform._check_billing_profile()


def test_checkout_offline_page_is_reloaded(platform, monkeypatch):
    page = platform.page
    page.set_content(OFFLINE_HTML)
    reloads = []

    def reload(**kwargs):
        reloads.append(kwargs)
        page.set_content(_checkout_html())

    monkeypatch.setattr(page, "reload", reload)
    platform._open_checkout_form()
    assert len(reloads) == 1


def test_checkout_offline_twice_gives_up(platform, monkeypatch):
    page = platform.page
    page.set_content(OFFLINE_HTML)
    monkeypatch.setattr(page, "reload", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="offline"):
        platform._open_checkout_form()


def test_offline_page_after_reserve_is_an_unknown_outcome(platform):
    page = platform.page
    page.set_content(
        """<button id="reservation-button-top"
             onclick="document.body.innerHTML = '<h1>You appear to be offline</h1>'">
           Reserve</button>"""
    )
    with pytest.raises(PaymentOutcomeUnknownError, match="offline' page after Reserve"):
        platform._reserve()
