"""The weather forecast: WMO labels, the range check (which makes no
request), Open-Meteo response parsing (mocked), hour filtering, several
places in one request, daylight saving, failures."""

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from date_planner.errors import ForecastFailed
from date_planner.planning import forecast

LA = ZoneInfo("America/Los_Angeles")
TODAY = date(2026, 10, 19)
DAY = date(2026, 10, 24)
SOLSTICE = (34.03765, -118.7477)


def stamp(day: date, hour: int, tz: ZoneInfo = LA) -> int:
    return int(datetime(day.year, day.month, day.day, hour, tzinfo=tz).timestamp())


def open_meteo(hours: range = range(24)) -> dict[str, Any]:
    """A canned Open-Meteo answer for DAY: warm and dry until a 17:00
    marine layer, with a shower at 20:00 and one missing reading."""
    n = len(hours)
    return {
        "latitude": 34.04,
        "longitude": -118.75,
        "timezone": "America/Los_Angeles",
        "hourly_units": {"temperature_2m": "°F"},
        "hourly": {
            "time": [stamp(DAY, h) for h in hours],
            "temperature_2m": [60.0 + h for h in hours],
            "precipitation_probability": [60 if h == 20 else 5 for h in hours],
            "precipitation": [0.12 if h == 20 else 0.0 for h in hours],
            "cloud_cover": [90 if h >= 17 else 10 for h in hours],
            "cloud_cover_low": [85 if h >= 17 else 0 for h in hours],
            "wind_speed_10m": [8.0] * n,
            "wind_gusts_10m": [30.0 if h == 15 else 12.0 for h in hours],
            "weather_code": [80 if h == 20 else 0 for h in hours[:-1]] + [None],
        },
    }


def flat(n: int = 24) -> dict[str, list[Any]]:
    """`n` hours of zeros: clear, 0°F, no wind."""
    return {name: [0] * n for name in forecast.HOURLY}


async def fetch(
    day: date = DAY,
    from_time: time | None = None,
    to_time: time | None = None,
) -> forecast.Forecast:
    """The forecast at Solstice Canyon."""
    place = forecast.Place(*SOLSTICE, LA, from_time, to_time)
    [result] = await forecast.fetch(day, [place], today=TODAY)
    return result


def test_wmo_codes_get_short_labels() -> None:
    assert forecast.conditions(0) == "clear"
    assert forecast.conditions(2) == "partly cloudy"
    assert forecast.conditions(45) == "fog"
    assert forecast.conditions(61) == "light rain"
    assert forecast.conditions(95) == "thunderstorm"
    assert forecast.conditions(42) == "unknown"
    assert forecast.conditions(None) is None


@pytest.mark.parametrize(
    ("day", "reason"),
    [
        (
            date(2026, 11, 10),
            "forecast covers up to 2026-11-03; check again from 2026-10-26",
        ),
        (date(2026, 10, 18), "2026-10-18 has passed"),
    ],
)
async def test_a_day_out_of_range_makes_no_request(day: date, reason: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(forecast.FORECAST_URL).respond(200, json=open_meteo())
        result = await fetch(day=day)
    assert not route.called
    assert not result.available
    assert result.reason is not None and result.reason.startswith(reason)
    assert result.hours == [] and result.fetched_at is None


@respx.mock
async def test_the_last_forecast_day_is_fetched() -> None:
    route = respx.get(forecast.FORECAST_URL).respond(200, json=open_meteo())
    result = await fetch(day=date(2026, 11, 3))
    assert route.called and result.available
    # Nothing past the horizon is asked for.
    assert route.calls.last.request.url.params["end_date"] == "2026-11-03"


@respx.mock
async def test_open_meteo_response_is_parsed() -> None:
    route = respx.get(forecast.FORECAST_URL).respond(200, json=open_meteo())
    result = await fetch()

    params = route.calls.last.request.url.params
    assert params["start_date"] == "2026-10-24"
    assert params["end_date"] == "2026-10-25"
    assert params["timezone"] == "America/Los_Angeles"
    assert params["timeformat"] == "unixtime"
    assert params["temperature_unit"] == "fahrenheit"
    assert params["wind_speed_unit"] == "mph"
    assert params["precipitation_unit"] == "inch"
    assert "cloud_cover_low" in params["hourly"].split(",")

    assert result.available and result.reason is None
    assert (result.lat, result.lng) == SOLSTICE
    assert result.fetched_at is not None and result.fetched_at.tzinfo == LA
    assert len(result.hours) == 24
    shower = result.hours[20]
    assert shower.time == datetime(2026, 10, 24, 20, 0, tzinfo=LA)
    assert shower.precip_probability == 60
    assert shower.precip_in == 0.12
    assert shower.cloud_cover_low == 85
    assert shower.conditions == "light showers"
    assert result.hours[23].conditions is None
    assert (result.high_f, result.low_f) == (83.0, 60.0)
    assert result.max_precip_probability == 60
    assert result.total_precip_in == 0.12


@respx.mock
async def test_hours_are_kept_to_the_stop() -> None:
    respx.get(forecast.FORECAST_URL).respond(200, json=open_meteo())
    result = await fetch(from_time=time(14, 30), to_time=time(16, 15))
    assert [h.time.hour for h in result.hours] == [14, 15, 16]
    assert result.max_precip_probability == 5
    assert result.total_precip_in == 0.0
    assert max(h.gust_mph or 0 for h in result.hours) == 30.0

    evening = await fetch(from_time=time(22, 0))
    assert [h.time.hour for h in evening.hours] == [22, 23]
    morning = await fetch(to_time=time(2, 0))
    assert [h.time.hour for h in morning.hours] == [0, 1]

    late = await fetch(from_time=time(22, 30), to_time=time(0, 30))
    assert [h.time.hour for h in late.hours] == [22, 23]
    to_midnight = await fetch(from_time=time(23, 0), to_time=time(0, 0))
    assert [h.time.hour for h in to_midnight.hours] == [23]


@respx.mock
async def test_unreachable_is_a_forecast_failure() -> None:
    respx.get(forecast.FORECAST_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ForecastFailed, match="failed"):
        await fetch()


@respx.mock
async def test_an_error_answer_carries_open_meteo_s_reason() -> None:
    respx.get(forecast.FORECAST_URL).respond(
        400, json={"error": True, "reason": "Latitude must be in range of -90 to 90°."}
    )
    with pytest.raises(ForecastFailed, match="400 Latitude must be"):
        await fetch()


@respx.mock
async def test_a_response_without_hours_is_a_forecast_failure() -> None:
    respx.get(forecast.FORECAST_URL).respond(200, json={"latitude": 34.0})
    with pytest.raises(ForecastFailed, match="no hourly"):
        await fetch()


@respx.mock
async def test_an_unreadable_hour_is_a_forecast_failure() -> None:
    data = open_meteo()
    data["hourly"]["time"][3] = "2026-10-24T03:00"
    respx.get(forecast.FORECAST_URL).respond(200, json=data)
    with pytest.raises(ForecastFailed, match="unreadable"):
        await fetch()


@respx.mock
async def test_the_fall_back_day_has_twenty_five_hours_each_read_right() -> None:
    """On 1 Nov the clocks go back at 2am: 01:00 happens twice, and the
    evening hours are PST. The day runs over into the next day's request."""
    fall_back = date(2026, 11, 1)
    midnight = stamp(fall_back, 0)
    times = [midnight + 3600 * i for i in range(49)]
    data = {"hourly": {"time": times, **flat(49)}}
    data["hourly"]["temperature_2m"] = list(range(49))
    respx.get(forecast.FORECAST_URL).respond(200, json=data)

    result = await fetch(day=fall_back)
    assert len(result.hours) == 25
    labels = [(h.time.hour, h.time.utcoffset()) for h in result.hours]
    pdt, pst = timedelta(hours=-7), timedelta(hours=-8)
    assert labels[:4] == [(0, pdt), (1, pdt), (1, pst), (2, pst)]
    assert labels[-1] == (23, pst)

    evening = await fetch(day=fall_back, from_time=time(19), to_time=time(21))
    # 19:00 PST is the 21st hour after midnight PDT.
    assert [(h.time.hour, h.temp_f) for h in evening.hours] == [(19, 20), (20, 21)]


@respx.mock
async def test_several_places_go_in_one_request() -> None:
    venice = (33.985, -118.4695)
    nyc = ZoneInfo("America/New_York")
    route = respx.get(forecast.FORECAST_URL).respond(
        200,
        json=[
            {"hourly": {"time": [stamp(DAY, h) for h in range(24)], **flat()}},
            {"hourly": {"time": [stamp(DAY, h) for h in range(24)], **flat()}},
            {"hourly": {"time": [stamp(DAY, h, nyc) for h in range(24)], **flat()}},
        ],
    )
    places = [
        forecast.Place(*SOLSTICE, LA, time(9), time(11)),
        forecast.Place(*venice, LA, time(18), time(19)),
        # The same trail again, later: it shares the first location.
        forecast.Place(*SOLSTICE, LA, time(16), time(17)),
        forecast.Place(40.7, -74.0, nyc, time(12), time(13)),
    ]
    results = await forecast.fetch(DAY, places, today=TODAY)

    assert route.call_count == 1
    params = route.calls.last.request.url.params
    assert params["latitude"] == "34.03765,33.985,40.7"
    assert params["longitude"] == "-118.7477,-118.4695,-74.0"
    assert params["timezone"] == (
        "America/Los_Angeles,America/Los_Angeles,America/New_York"
    )
    assert [[h.time.hour for h in r.hours] for r in results] == [
        [9, 10],
        [18],
        [16],
        [12],
    ]
    assert [(r.lat, r.lng) for r in results[:2]] == [SOLSTICE, venice]
    assert results[3].timezone == "America/New_York"
    assert results[3].hours[0].time.tzinfo == nyc


async def test_places_all_out_of_range_make_no_request() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(forecast.FORECAST_URL).respond(200, json=[])
        results = await forecast.fetch(
            date(2026, 12, 1),
            [forecast.Place(*SOLSTICE, LA), forecast.Place(34.0, -118.5, LA)],
            today=TODAY,
        )
    assert not route.called
    assert [r.available for r in results] == [False, False]
    assert await forecast.fetch(DAY, [], today=TODAY) == []


@respx.mock
async def test_an_answer_for_the_wrong_number_of_places_is_a_failure() -> None:
    respx.get(forecast.FORECAST_URL).respond(200, json=open_meteo())
    places = [forecast.Place(*SOLSTICE, LA), forecast.Place(34.0, -118.5, LA)]
    with pytest.raises(ForecastFailed, match="wrong number"):
        await forecast.fetch(DAY, places, today=TODAY)
