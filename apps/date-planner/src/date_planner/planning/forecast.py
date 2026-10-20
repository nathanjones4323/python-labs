"""The hourly weather forecast at a place, from Open-Meteo.

Free and keyless. Open-Meteo forecasts 16 days counting today, so a day
more than `HORIZON_DAYS` out (or already past) gets no request at all: it
comes back unavailable with the day to ask again from. Nothing is cached or
stored — a forecast is only worth having fresh.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from date_planner.config import get_settings
from date_planner.errors import ForecastFailed

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# Today plus 15: Open-Meteo's 16 forecast days.
HORIZON_DAYS = 15
HOURLY = (
    "temperature_2m",
    "precipitation_probability",
    "precipitation",
    "cloud_cover",
    "cloud_cover_low",
    "wind_speed_10m",
    "wind_gusts_10m",
    "weather_code",
)

# WMO weather interpretation codes, as Open-Meteo documents them.
CONDITIONS = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "heavy showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}


@dataclass(frozen=True)
class Hour:
    """One forecast hour, starting at `time`. Any value may be missing."""

    time: datetime
    temp_f: float | None
    precip_probability: int | None
    precip_in: float | None
    cloud_cover: int | None
    cloud_cover_low: int | None
    wind_mph: float | None
    gust_mph: float | None
    conditions: str | None


@dataclass(frozen=True)
class Forecast:
    day: date
    timezone: str
    available: bool
    reason: str | None = None
    fetched_at: datetime | None = None
    hours: list[Hour] = field(default_factory=list)
    high_f: float | None = None
    low_f: float | None = None
    max_precip_probability: int | None = None
    total_precip_in: float | None = None


def conditions(code: int | None) -> str | None:
    """A short label for a WMO weather code: "clear", "light rain", …"""
    if code is None:
        return None
    return CONDITIONS.get(code, "unknown")


def last_day(today: date) -> date:
    """The furthest day the forecast reaches from `today`."""
    return today + timedelta(days=HORIZON_DAYS)


def out_of_range(day: date, today: date) -> str | None:
    """Why there's no forecast for `day` yet, or None if there is one."""
    if day < today:
        return f"{day} has passed; there is no forecast for it"
    if day > last_day(today):
        return (
            f"forecast covers up to {last_day(today)}; "
            f"check again from {day - timedelta(days=HORIZON_DAYS)}"
        )
    return None


async def fetch(
    lat: float,
    lng: float,
    day: date,
    tz: ZoneInfo,
    from_time: time | None = None,
    to_time: time | None = None,
    *,
    today: date | None = None,
    client: httpx.AsyncClient | None = None,
) -> Forecast:
    """`day`'s hourly forecast at (`lat`, `lng`), in `tz`.

    `from_time` / `to_time` keep the hours overlapping that stretch of the
    day: 14:30–16:15 keeps the 14:00, 15:00 and 16:00 hours. The summary is
    over the hours kept.
    """
    today = today or datetime.now(tz).date()
    reason = out_of_range(day, today)
    if reason is not None:
        return Forecast(day=day, timezone=tz.key, available=False, reason=reason)

    params = {
        "latitude": lat,
        "longitude": lng,
        "hourly": ",".join(HOURLY),
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": tz.key,
        "start_date": day.isoformat(),
        "end_date": day.isoformat(),
    }
    data = await _get_json(client, params)
    fetched_at = datetime.now(tz)
    hours = [h for h in parse_hours(data, tz) if _overlaps(h.time, from_time, to_time)]
    return summarise(day, tz, hours, fetched_at)


def parse_hours(data: dict[str, Any], tz: ZoneInfo) -> list[Hour]:
    try:
        hourly = data["hourly"]
        times = hourly["time"]
        columns = {name: hourly[name] for name in HOURLY}
    except (KeyError, TypeError) as exc:
        raise ForecastFailed("Open-Meteo returned no hourly forecast") from exc

    def value(name: str, i: int) -> Any:
        column = columns[name]
        return column[i] if i < len(column) else None

    hours = []
    for i, stamp in enumerate(times):
        code = value("weather_code", i)
        hours.append(
            Hour(
                time=datetime.fromisoformat(stamp).replace(tzinfo=tz),
                temp_f=value("temperature_2m", i),
                precip_probability=value("precipitation_probability", i),
                precip_in=value("precipitation", i),
                cloud_cover=value("cloud_cover", i),
                cloud_cover_low=value("cloud_cover_low", i),
                wind_mph=value("wind_speed_10m", i),
                gust_mph=value("wind_gusts_10m", i),
                conditions=conditions(code),
            )
        )
    return hours


def summarise(
    day: date, tz: ZoneInfo, hours: list[Hour], fetched_at: datetime
) -> Forecast:
    temps = [h.temp_f for h in hours if h.temp_f is not None]
    chances = [h.precip_probability for h in hours if h.precip_probability is not None]
    precip = [h.precip_in for h in hours if h.precip_in is not None]
    return Forecast(
        day=day,
        timezone=tz.key,
        available=True,
        fetched_at=fetched_at,
        hours=hours,
        high_f=max(temps, default=None),
        low_f=min(temps, default=None),
        max_precip_probability=max(chances, default=None),
        total_precip_in=round(sum(precip), 2) if precip else None,
    )


def _overlaps(start: datetime, from_time: time | None, to_time: time | None) -> bool:
    """Whether the hour starting at `start` overlaps from_time–to_time."""
    hour_start = start.time()
    hour_end = (start + timedelta(hours=1)).time()
    if from_time is not None and hour_end != time(0) and hour_end <= from_time:
        return False
    return to_time is None or hour_start < to_time


async def _get_json(
    client: httpx.AsyncClient | None, params: dict[str, Any]
) -> dict[str, Any]:
    try:
        if client is not None:
            response = await client.get(FORECAST_URL, params=params)
        else:
            async with httpx.AsyncClient(timeout=get_settings().fetch_timeout) as owned:
                response = await owned.get(FORECAST_URL, params=params)
    except httpx.HTTPError as exc:
        raise ForecastFailed(f"Weather forecast request failed: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.is_error:
        # Open-Meteo explains a bad request in `reason`.
        detail = data.get("reason") if isinstance(data, dict) else None
        raise ForecastFailed(
            f"Weather forecast request failed: {response.status_code}"
            + (f" {detail}" if detail else "")
        )
    if not isinstance(data, dict):
        raise ForecastFailed("Open-Meteo returned something other than a JSON object")
    return data
