"""Health and training metrics, a small named set, one day at a time.

Each metric is a separate Garmin call per day, so a request is capped
(`MAX_CALLS`). The normalizers pull a few headline numbers out of Garmin's
responses; the full responses (minute-by-minute HR, sleep stages, stress
curves) are never returned or logged. A metric Garmin has nothing for that
day is null.
"""

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from garmin_mcp.errors import InvalidInput
from garmin_mcp.repositories.garmin_client import JSON, GarminRepository
from garmin_mcp.schemas.results import DailyMetrics, Metric, TrainingMetrics

DEFAULT_METRICS: list[Metric] = ["daily_summary", "sleep", "training_readiness"]
MAX_DAYS = 14
MAX_CALLS = 42

UNITS = {
    "_seconds": "seconds",
    "_bpm": "beats per minute",
    "_ms": "milliseconds",
    "steps": "count",
    "kilocalories": "kcal",
    "minutes": "minutes",
    "score": "0–100",
    "body_battery": "0–100",
    "stress": "0–100",
    "vo2max": "ml/kg/min",
    "load": "Garmin training load units",
}


class HealthService:
    def __init__(self, repo: GarminRepository) -> None:
        self._repo = repo
        self._readers: dict[Metric, Callable[[date], dict[str, Any] | None]] = {
            "daily_summary": lambda d: _daily(self._repo.get_daily_summary(d)),
            "sleep": lambda d: _sleep(self._repo.get_sleep(d)),
            "hrv": lambda d: _hrv(self._repo.get_hrv(d)),
            "training_readiness": lambda d: _readiness(
                self._repo.get_training_readiness(d)
            ),
            "training_status": lambda d: _training_status(
                self._repo.get_training_status(d)
            ),
            "vo2max": lambda d: _vo2max(self._repo.get_max_metrics(d)),
        }

    def metrics(
        self, start: date, end: date | None, metrics: list[Metric] | None
    ) -> TrainingMetrics:
        end = end or start
        chosen: list[Metric] = list(dict.fromkeys(metrics or DEFAULT_METRICS))
        if end < start:
            raise InvalidInput("end_date is before start_date")
        days = (end - start).days + 1
        if days > MAX_DAYS:
            raise InvalidInput(f"ask for at most {MAX_DAYS} days at a time")
        if days * len(chosen) > MAX_CALLS:
            raise InvalidInput(
                f"{days} days x {len(chosen)} metrics is {days * len(chosen)} Garmin "
                f"calls; the limit is {MAX_CALLS}. Ask for fewer days or metrics."
            )
        out = []
        for offset in range(days):
            day = start + timedelta(days=offset)
            out.append(
                DailyMetrics(
                    date=day, values={m: self._readers[m](day) for m in chosen}
                )
            )
        return TrainingMetrics(metrics=chosen, units=UNITS, days=out)


def _compact(values: dict[str, Any]) -> dict[str, Any] | None:
    kept = {k: v for k, v in values.items() if v is not None}
    return kept or None


def _daily(raw: JSON | None) -> dict[str, Any] | None:
    if not raw:
        return None
    return _compact(
        {
            "steps": raw.get("totalSteps"),
            "resting_hr_bpm": raw.get("restingHeartRate"),
            "average_stress": raw.get("averageStressLevel"),
            "body_battery_high": raw.get("bodyBatteryHighestValue"),
            "body_battery_low": raw.get("bodyBatteryLowestValue"),
            "active_kilocalories": raw.get("activeKilocalories"),
            "moderate_intensity_minutes": raw.get("moderateIntensityMinutes"),
            "vigorous_intensity_minutes": raw.get("vigorousIntensityMinutes"),
        }
    )


def _sleep(raw: JSON | None) -> dict[str, Any] | None:
    dto = (raw or {}).get("dailySleepDTO") or {}
    score = ((dto.get("sleepScores") or {}).get("overall") or {}).get("value")
    return _compact(
        {
            "sleep_seconds": dto.get("sleepTimeSeconds"),
            "deep_seconds": dto.get("deepSleepSeconds"),
            "light_seconds": dto.get("lightSleepSeconds"),
            "rem_seconds": dto.get("remSleepSeconds"),
            "awake_seconds": dto.get("awakeSleepSeconds"),
            "sleep_score": score,
        }
    )


def _hrv(raw: JSON | None) -> dict[str, Any] | None:
    summary = (raw or {}).get("hrvSummary") or {}
    return _compact(
        {
            "last_night_average_ms": summary.get("lastNightAvg"),
            "weekly_average_ms": summary.get("weeklyAvg"),
            "status": summary.get("status"),
        }
    )


def _readiness(raw: Any) -> dict[str, Any] | None:
    entry = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(entry, dict):
        return None
    return _compact(
        {"readiness_score": entry.get("score"), "level": entry.get("level")}
    )


def _training_status(raw: JSON | None) -> dict[str, Any] | None:
    latest = ((raw or {}).get("mostRecentTrainingStatus") or {}).get(
        "latestTrainingStatusData"
    ) or {}
    # Keyed by device id; the primary training device is the one that counts,
    # and there is normally only one.
    data = next(iter(latest.values()), None) if isinstance(latest, dict) else None
    if not isinstance(data, dict):
        return None
    load = data.get("acuteTrainingLoadDTO") or {}
    return _compact(
        {
            "status": data.get("trainingStatusFeedbackPhrase"),
            "acute_load": load.get("dailyTrainingLoadAcute"),
            "chronic_load": load.get("dailyTrainingLoadChronic"),
        }
    )


def _vo2max(raw: Any) -> dict[str, Any] | None:
    entry = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(entry, dict):
        return None
    generic = entry.get("generic") or {}
    cycling = entry.get("cycling") or {}
    return _compact(
        {
            "running_vo2max": generic.get("vo2MaxPreciseValue")
            or generic.get("vo2MaxValue"),
            "cycling_vo2max": cycling.get("vo2MaxPreciseValue")
            or cycling.get("vo2MaxValue"),
        }
    )
