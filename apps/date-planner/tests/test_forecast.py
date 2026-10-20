"""The weather forecast: WMO labels, the range check (which makes no
request), Open-Meteo response parsing (mocked), hour filtering, failures."""

from datetime import date, datetime, time
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
            "time": [f"{DAY}T{h:02d}:00" for h in hours],
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


async def fetch(**kwargs: Any) -> forecast.Forecast:
    args: dict[str, Any] = {"day": DAY, "today": TODAY} | kwargs
    return await forecast.fetch(*SOLSTICE, tz=LA, **args)


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


@respx.mock
async def test_open_meteo_response_is_parsed() -> None:
    route = respx.get(forecast.FORECAST_URL).respond(200, json=open_meteo())
    result = await fetch()

    params = route.calls.last.request.url.params
    assert params["start_date"] == params["end_date"] == "2026-10-24"
    assert params["timezone"] == "America/Los_Angeles"
    assert params["temperature_unit"] == "fahrenheit"
    assert params["wind_speed_unit"] == "mph"
    assert params["precipitation_unit"] == "inch"
    assert "cloud_cover_low" in params["hourly"].split(",")

    assert result.available and result.reason is None
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
