"""The hourly weather forecast at a handful of places, from Open-Meteo.

Free and keyless. Open-Meteo forecasts 16 days counting today, so a day
more than `HORIZON_DAYS` out (or already past) gets no request at all: it
comes back unavailable with the day to ask again from. Every place in range
goes in one request, whatever the number of stops. Nothing is cached or
stored — a forecast is only worth having fresh.

Hours come back as Unix timestamps rather than local labels: Open-Meteo
labels a whole day with one UTC offset, so on a daylight-saving change the
labels after 2am are an hour out.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from date_planner.errors import ForecastFailed
from date_planner.json_api import request_json

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
class Place:
    """Where to forecast, and the stretch of the day to keep.

    `from_time` / `to_time` keep the hours overlapping that stretch: 14:30–
    16:15 keeps the 14:00, 15:00 and 16:00 hours. A `to_time` of midnight,
    or one before `from_time` (a stop running past midnight), runs to the
    end of the day.
    """

    lat: float
    lng: float
    tz: ZoneInfo
    from_time: time | None = None
    to_time: time | None = None


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
    lat: float
    lng: float
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
    day: date,
    places: Sequence[Place],
    *,
    today: date | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[Forecast]:
    """`day`'s hourly forecast at each of `places`, in the same order and
    each in its own timezone. The summary is over the hours kept."""
    todays = [today or datetime.now(place.tz).date() for place in places]
    reasons = [out_of_range(day, t) for t in todays]
    wanted = [p for p, reason in zip(places, reasons) if reason is None]
    # Stops at the same venue share one location in the request.
    locations = list(dict.fromkeys((p.lat, p.lng, p.tz.key) for p in wanted))
    days: dict[tuple[float, float, str], list[Hour]] = {}
    if locations:
        # The next day too: a fall-back day has 25 hours, so its last one
        # starts after 24 hours from midnight.
        through = day + timedelta(days=1)
        if through > last_day(min(todays)):
            through = day
        data = await _get_json(client, locations, day, through)
        # One location comes back as an object, several as a list.
        answers = data if isinstance(data, list) else [data]
        if len(answers) != len(locations):
            raise ForecastFailed("Open-Meteo answered for the wrong number of places")
        for location, answer in zip(locations, answers):
            tz = ZoneInfo(location[2])
            days[location] = [
                h for h in parse_hours(answer, tz) if h.time.date() == day
            ]
    fetched_at = datetime.now(UTC)

    results = []
    for place, reason in zip(places, reasons):
        if reason is not None:
            results.append(
                Forecast(
                    day=day,
                    lat=place.lat,
                    lng=place.lng,
                    timezone=place.tz.key,
                    available=False,
                    reason=reason,
                )
            )
            continue
        hours = [
            h
            for h in days[(place.lat, place.lng, place.tz.key)]
            if _overlaps(h.time, place.from_time, place.to_time)
        ]
        results.append(summarise(day, place, hours, fetched_at.astimezone(place.tz)))
    return results


def parse_hours(data: Any, tz: ZoneInfo) -> list[Hour]:
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
    try:
        for i, stamp in enumerate(times):
            if isinstance(stamp, bool) or not isinstance(stamp, int | float):
                raise TypeError(f"not a timestamp: {stamp!r}")
            code = value("weather_code", i)
            hours.append(
                Hour(
                    time=datetime.fromtimestamp(stamp, tz),
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
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise ForecastFailed(
            "Open-Meteo returned an unreadable hourly forecast"
        ) from exc
    return hours


def summarise(
    day: date, place: Place, hours: list[Hour], fetched_at: datetime
) -> Forecast:
    temps = [h.temp_f for h in hours if h.temp_f is not None]
    chances = [h.precip_probability for h in hours if h.precip_probability is not None]
    precip = [h.precip_in for h in hours if h.precip_in is not None]
    return Forecast(
        day=day,
        lat=place.lat,
        lng=place.lng,
        timezone=place.tz.key,
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
    if to_time is None or to_time == time(0):
        return True
    if from_time is not None and to_time < from_time:
        return True
    return hour_start < to_time


async def _get_json(
    client: httpx.AsyncClient | None,
    locations: list[tuple[float, float, str]],
    day: date,
    through: date,
) -> Any:
    params = {
        "latitude": ",".join(str(lat) for lat, _, _ in locations),
        "longitude": ",".join(str(lng) for _, lng, _ in locations),
        "timezone": ",".join(tz for _, _, tz in locations),
        "hourly": ",".join(HOURLY),
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timeformat": "unixtime",
        "start_date": day.isoformat(),
        "end_date": through.isoformat(),
    }
    return await request_json(
        client,
        "GET",
        FORECAST_URL,
        failure=ForecastFailed,
        service="Weather forecast",
        # Open-Meteo explains a bad request in `reason`.
        reason=lambda body: body.get("reason") if isinstance(body, dict) else None,
        params=params,
    )
