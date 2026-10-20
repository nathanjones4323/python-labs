"""Recorded activities, summarized. Raw Garmin activity JSON runs to hundreds
of fields (and full GPS/HR streams in the details call); these keep the
handful an agent reasons about."""

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from garmin_mcp.errors import InvalidInput
from garmin_mcp.repositories.garmin_client import JSON, GarminRepository
from garmin_mcp.schemas.results import (
    ActivityDetails,
    ActivitySummary,
    Lap,
)
from garmin_mcp.services.activity_sets import _num, read_sets

MAX_LIMIT = 100
MAX_LAPS = 50
DEFAULT_DAYS = 14
MAX_RANGE_DAYS = 366


class ActivityService:
    def __init__(
        self, repo: GarminRepository, today: Callable[[], date] = date.today
    ) -> None:
        self._repo = repo
        self._today = today

    def recent(
        self,
        start: date | None,
        end: date | None,
        activity_type: str | None,
        limit: int,
    ) -> list[ActivitySummary]:
        end = end or self._today()
        start = start or end - timedelta(days=DEFAULT_DAYS)
        if end < start:
            raise InvalidInput("end_date is before start_date")
        if (end - start).days > MAX_RANGE_DAYS:
            raise InvalidInput(f"ask for at most {MAX_RANGE_DAYS} days at a time")
        if not 1 <= limit <= MAX_LIMIT:
            raise InvalidInput(f"limit must be 1–{MAX_LIMIT}")
        parent, subtype = self._type_filter(activity_type)
        raw = self._repo.list_activities(start, end, parent, subtype, limit)
        return [_summary(a, a.get("activityType")) for a in raw[:limit]]

    def _type_filter(self, type_key: str | None) -> tuple[str | None, str | None]:
        """Garmin's search takes a top-level type (running) as activityType,
        and refuses a subtype (strength_training, trail_running) there with
        a 400: a subtype goes in activitySubType, next to its parent."""
        if type_key is None:
            return None, None
        types = self._repo.list_activity_types()
        by_id = {t.get("typeId"): t for t in types}
        match = next((t for t in types if t.get("typeKey") == type_key), None)
        if match is None:
            raise InvalidInput(
                f"unknown activity type {type_key!r}; use a Garmin type key "
                "such as running, cycling or strength_training"
            )
        parent = by_id.get(match.get("parentTypeId"))
        if parent is None or parent.get("parentTypeId") is None:
            # A top-level type: its parent is the root ("all"), or it has none.
            return type_key, None
        return parent["typeKey"], type_key

    def details(self, activity_id: int) -> ActivityDetails:
        raw = self._repo.get_activity(activity_id)
        summary = raw.get("summaryDTO") or {}
        kind = raw.get("activityTypeDTO") or raw.get("activityType")
        base = _summary(raw | summary, kind)
        details = ActivityDetails(
            **base.model_dump(),
            description=raw.get("description"),
            moving_duration_seconds=_num(summary.get("movingDuration")),
            average_cadence=_num(
                summary.get("averageRunCadence") or summary.get("averageBikeCadence")
            ),
            training_effect_aerobic=_num(summary.get("trainingEffect")),
            training_effect_anaerobic=_num(summary.get("anaerobicTrainingEffect")),
        )
        if details.activity_type == "strength_training":
            details.exercise_sets = read_sets(
                self._repo.get_activity_exercise_sets(activity_id)
            )
        else:
            details.laps = _laps(self._repo.get_activity_splits(activity_id))
        return details


def _summary(a: JSON, kind: Any) -> ActivitySummary:
    return ActivitySummary(
        activity_id=int(a["activityId"]),
        name=a.get("activityName"),
        activity_type=(kind or {}).get("typeKey") if isinstance(kind, dict) else None,
        start_time_local=a.get("startTimeLocal"),
        duration_seconds=_num(a.get("duration")),
        distance_meters=_num(a.get("distance")),
        calories=_num(a.get("calories")),
        average_hr=_num(a.get("averageHR")),
        max_hr=_num(a.get("maxHR")),
        average_speed_mps=_num(a.get("averageSpeed")),
        elevation_gain_meters=_num(a.get("elevationGain")),
    )


def _laps(splits: JSON) -> list[Lap]:
    laps = splits.get("lapDTOs") or []
    return [
        Lap(
            index=int(lap.get("lapIndex") or i),
            duration_seconds=_num(lap.get("duration")),
            distance_meters=_num(lap.get("distance")),
            average_hr=_num(lap.get("averageHR")),
            average_speed_mps=_num(lap.get("averageSpeed")),
        )
        for i, lap in enumerate(laps[:MAX_LAPS], start=1)
    ]
