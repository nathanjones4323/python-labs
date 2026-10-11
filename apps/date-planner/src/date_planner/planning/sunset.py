"""When the sun goes down at a place, computed locally with `astral`.

No network: the sun's position is arithmetic. Checked against the US Naval
Observatory for Malibu on 2026-10-24 (set 18:10, civil twilight ends 18:35
PDT); astral lands within a minute of both.
"""

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from astral import Observer, SunDirection
from astral.sun import golden_hour, sun


@dataclass(frozen=True)
class Sunset:
    sunset: datetime
    # Sun 6° below the horizon: the end of usable light on a trail.
    civil_dusk: datetime
    # When the evening golden hour begins — the light worth a dinner table.
    golden_hour_start: datetime


def compute(lat: float, lng: float, day: date, tz: ZoneInfo) -> Sunset:
    """Sunset, civil dusk and golden hour on `day` at (`lat`, `lng`), in `tz`."""
    observer = Observer(latitude=lat, longitude=lng)
    times = sun(observer, date=day, tzinfo=tz)
    golden_start, _ = golden_hour(
        observer, date=day, direction=SunDirection.SETTING, tzinfo=tz
    )
    return Sunset(
        sunset=times["sunset"],
        civil_dusk=times["dusk"],
        golden_hour_start=golden_start,
    )
