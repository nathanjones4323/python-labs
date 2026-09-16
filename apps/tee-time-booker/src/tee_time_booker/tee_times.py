"""Fetching and choosing a tee time from foreUP's booking API."""

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

from tee_time_booker import config


@dataclass(frozen=True)
class TeeTime:
    start: datetime  # naive, course-local
    green_fee: float | None
    holes: int | None
    max_players: int | None

    @property
    def ui_label(self) -> str:
        """Time as rendered in foreUP's slot list, e.g. '8:00am', '1:30pm'."""
        return self.start.strftime("%I:%M%p").lstrip("0").lower()


def api_query_params(target: date) -> dict[str, str]:
    return {
        "time": "all",
        "date": target.strftime(config.FOREUP_DATE_FORMAT),
        "holes": str(config.HOLES),
        "players": str(config.PLAYERS),
        "booking_class": config.BOOKING_CLASS,
        "schedule_id": config.SCHEDULE_ID,
        "schedule_ids[]": config.SCHEDULE_ID,
        "specials_only": "0",
        "api_key": "no_limits",
    }


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_tee_times(payload: Any) -> list[TeeTime]:
    """Turn the API's JSON list into TeeTime objects. Unparseable rows are skipped."""
    if not isinstance(payload, list):
        raise ValueError(f"Unexpected tee time payload: {payload!r}")

    tee_times: list[TeeTime] = []
    for row in payload:
        if not isinstance(row, dict) or "time" not in row:
            continue
        try:
            start = datetime.strptime(str(row["time"]), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        tee_times.append(
            TeeTime(
                start=start,
                green_fee=_to_float(row.get("green_fee")),
                holes=_to_int(row.get("holes")),
                max_players=_to_int(row.get("maximum_players_per_booking")),
            )
        )
    return tee_times


def choose_tee_time(
    tee_times: list[TeeTime],
    earliest: time = config.EARLIEST_TEE_TIME,
    latest: time = config.LATEST_TEE_TIME,
    prefer_earlier: bool = config.PREFER_EARLIER,
) -> TeeTime:
    """Pick the earliest (or latest) slot starting within [earliest, latest]."""
    candidates = [t for t in tee_times if earliest <= t.start.time() <= latest]
    if not candidates:
        raise ValueError(
            f"No tee times available between {earliest:%I:%M %p} "
            f"and {latest:%I:%M %p}. Try adjusting the time window."
        )
    candidates.sort(key=lambda t: t.start, reverse=not prefer_earlier)
    return candidates[0]
