"""An in-memory Garmin account implementing `GarminRepository`.

It keeps Garmin's JSON shapes (as far as the services read them) so the
codec and normalizers run for real. Every write is recorded in `writes`, so
a test can assert that nothing was sent, or that something was sent exactly
once. `fail_next_write` makes the next write raise; with `apply=True` the
change lands first, which is what a timeout after Garmin committed looks
like.
"""

import copy
import itertools
from dataclasses import dataclass
from datetime import date
from typing import Any

from garmin_mcp.errors import (
    GarminApiError,
    NotFound,
    WriteOutcomeUnknown,
    WriteRejected,
)

JSON = dict[str, Any]


# A slice of Garmin's activity type tree (get_activity_types).
ACTIVITY_TYPES: list[JSON] = [
    {"typeId": 17, "typeKey": "all", "parentTypeId": None},
    {"typeId": 1, "typeKey": "running", "parentTypeId": 17},
    {"typeId": 6, "typeKey": "trail_running", "parentTypeId": 1},
    {"typeId": 2, "typeKey": "cycling", "parentTypeId": 17},
    {"typeId": 29, "typeKey": "fitness_equipment", "parentTypeId": 17},
    {"typeId": 13, "typeKey": "strength_training", "parentTypeId": 29},
]


def _parent_key(t: JSON) -> str | None:
    by_id = {x["typeId"]: x["typeKey"] for x in ACTIVITY_TYPES}
    return by_id.get(t["parentTypeId"])


@dataclass
class Failure:
    error: WriteRejected | WriteOutcomeUnknown
    apply: bool = False


class FakeRepository:
    def __init__(self) -> None:
        self._ids = itertools.count(1000)
        self.workouts: dict[int, JSON] = {}
        self.calendar: list[JSON] = []
        self.activities: list[JSON] = []
        self.exercise_sets: dict[int, JSON] = {}
        self.devices: list[JSON] = [
            {
                "deviceId": 3001,
                "displayName": "Venu 4",
                "productDisplayName": "Venu 4 - 45mm",
                "deviceTypeSimpleName": "Garmin Venu 4",
                "workoutCapable": True,
                "strengthWorkoutCapable": True,
                "runningWorkoutCapable": True,
                "maxWorkoutCount": 200,
            },
            {
                "deviceId": 3002,
                "displayName": "HRM-Pro",
                "productDisplayName": "HRM-Pro",
            },
        ]
        self.last_used: JSON = {"userDeviceId": 3001, "lastUsedDeviceName": "Venu 4"}
        self.messages: list[JSON] = []
        self.writes: list[tuple[str, Any]] = []
        self.reads: list[str] = []
        self.fail_next_write: Failure | None = None
        # Applied to a workout as Garmin "stores" it, to simulate Garmin
        # changing what it was sent.
        self.mangle: Any = None
        self.mangle_sets: Any = None
        self.queue_pushes = True
        self.list_activities_limits: list[int] = []
        self.metrics: dict[tuple[str, date], Any] = {}

    # -- helpers for tests ------------------------------------------------

    def add_workout(self, raw: JSON) -> int:
        workout_id = next(self._ids)
        self.workouts[workout_id] = copy.deepcopy(raw) | {"workoutId": workout_id}
        return workout_id

    def add_calendar_entry(self, workout_id: int, day: date) -> int:
        entry_id = next(self._ids)
        workout = self.workouts.get(workout_id, {})
        self.calendar.append(
            {
                "id": entry_id,
                "itemType": "workout",
                "date": day.isoformat(),
                "title": workout.get("workoutName"),
                "workoutId": workout_id,
                "sportTypeKey": (workout.get("sportType") or {}).get("sportTypeKey"),
            }
        )
        return entry_id

    def _write(self, name: str, args: Any, apply: Any) -> Any:
        self.writes.append((name, args))
        failure, self.fail_next_write = self.fail_next_write, None
        if failure is not None:
            if failure.apply:
                apply()
            raise failure.error
        return apply()

    # -- account ----------------------------------------------------------

    def get_profile(self) -> JSON:
        return {
            "display_name": "runner",
            "full_name": "A Runner",
            "unit_system": "metric",
        }

    # -- activities -------------------------------------------------------

    def list_activity_types(self) -> list[JSON]:
        return ACTIVITY_TYPES

    def list_activities(
        self,
        start: date,
        end: date,
        activity_type: str | None,
        activity_subtype: str | None,
        limit: int,
    ) -> list[JSON]:
        # Garmin's rules: activityType must be top level, and activitySubType
        # must be a child of it.
        parents = {t["typeKey"]: _parent_key(t) for t in ACTIVITY_TYPES}
        if activity_type is not None and parents.get(activity_type) != "all":
            raise GarminApiError("Activity type cannot be an activity sub type")
        if activity_subtype is not None and parents.get(activity_subtype) != (
            activity_type
        ):
            raise GarminApiError("activity sub type doesn't belong to activity type")
        self.list_activities_limits.append(limit)

        def matches(a: JSON) -> bool:
            key = a["activityType"]["typeKey"]
            if activity_subtype is not None:
                return bool(key == activity_subtype)
            return activity_type is None or activity_type in (key, parents.get(key))

        return [
            a
            for a in self.activities
            if start.isoformat() <= a["startTimeLocal"][:10] <= end.isoformat()
            and matches(a)
        ][:limit]

    def get_activity(self, activity_id: int) -> JSON:
        for a in self.activities:
            if a["activityId"] == activity_id:
                return {
                    "activityId": activity_id,
                    "activityName": a.get("activityName"),
                    "activityTypeDTO": a["activityType"],
                    "summaryDTO": {
                        k: v for k, v in a.items() if k not in ("activityId",)
                    },
                }
        raise NotFound("activity", "Garmin has no such item.")

    def get_activity_splits(self, activity_id: int) -> JSON:
        return {"lapDTOs": [{"lapIndex": 1, "duration": 300.0, "distance": 1000.0}]}

    def get_activity_exercise_sets(self, activity_id: int) -> JSON:
        self.get_activity(activity_id)
        stored = self.exercise_sets.get(activity_id)
        if stored is None:
            stored = {
                "activityId": activity_id,
                "exerciseSets": [
                    {
                        "setType": "ACTIVE",
                        "exercises": [
                            {
                                "category": "BENCH_PRESS",
                                "name": "BARBELL_BENCH_PRESS",
                                "probability": 99.6,
                            }
                        ],
                        "repetitionCount": 8,
                        "weight": 60000.0,
                        "duration": 30.0,
                        "startTime": "2026-10-15T14:00:00.0",
                    },
                    {
                        "setType": "REST",
                        "exercises": [],
                        "duration": 90.0,
                        "startTime": "2026-10-15T14:00:30.0",
                    },
                ],
            }
        return copy.deepcopy(stored)

    def set_activity_exercise_sets(self, activity_id: int, payload: JSON) -> Any:
        def apply() -> None:
            stored = copy.deepcopy(payload)
            if self.mangle_sets:
                self.mangle_sets(stored)
            self.exercise_sets[activity_id] = stored

        return self._write("set_activity_exercise_sets", (activity_id, payload), apply)

    # -- metrics ----------------------------------------------------------

    def _metric(self, name: str, day: date) -> Any:
        self.reads.append(name)
        return self.metrics.get((name, day))

    def get_daily_summary(self, day: date) -> JSON | None:
        return self._metric("daily_summary", day)  # type: ignore[no-any-return]

    def get_sleep(self, day: date) -> JSON | None:
        return self._metric("sleep", day)  # type: ignore[no-any-return]

    def get_hrv(self, day: date) -> JSON | None:
        return self._metric("hrv", day)  # type: ignore[no-any-return]

    def get_training_readiness(self, day: date) -> Any:
        return self._metric("training_readiness", day)

    def get_training_status(self, day: date) -> JSON | None:
        return self._metric("training_status", day)  # type: ignore[no-any-return]

    def get_max_metrics(self, day: date) -> Any:
        return self._metric("vo2max", day)

    # -- workouts ---------------------------------------------------------

    def list_workouts(self, limit: int, sport_keys: list[str] | None) -> list[JSON]:
        items = [
            copy.deepcopy(w)
            for w in reversed(self.workouts.values())
            if sport_keys is None or w["sportType"]["sportTypeKey"] in sport_keys
        ]
        return items[:limit]

    def get_workout(self, workout_id: int) -> JSON:
        if workout_id not in self.workouts:
            raise NotFound("workout", "Garmin has no such item.")
        return copy.deepcopy(self.workouts[workout_id])

    def _store(self, workout_id: int, payload: JSON) -> JSON:
        stored = copy.deepcopy(payload) | {"workoutId": workout_id, "ownerId": 42}
        if self.mangle:
            self.mangle(stored)
        self.workouts[workout_id] = stored
        return copy.deepcopy(stored)

    def create_workout(self, payload: JSON) -> JSON:
        return self._write(  # type: ignore[no-any-return]
            "create_workout", payload, lambda: self._store(next(self._ids), payload)
        )

    def update_workout(self, workout_id: int, payload: JSON) -> JSON:
        return self._write(  # type: ignore[no-any-return]
            "update_workout",
            (workout_id, payload),
            lambda: self._store(workout_id, payload),
        )

    # -- calendar ---------------------------------------------------------

    def get_calendar_month(self, year: int, month: int) -> JSON:
        prefix = f"{year:04d}-{month:02d}"
        return {
            "calendarItems": [
                copy.deepcopy(i) for i in self.calendar if i["date"].startswith(prefix)
            ]
        }

    def get_scheduled_workout(self, scheduled_workout_id: int) -> JSON:
        for item in self.calendar:
            if item["id"] == scheduled_workout_id:
                workout = self.workouts.get(item["workoutId"], {})
                return {
                    "workoutScheduleId": item["id"],
                    "calendarDate": item["date"],
                    "workout": {
                        "workoutId": item["workoutId"],
                        "workoutName": workout.get("workoutName"),
                    },
                }
        raise NotFound("calendar_entry", "Garmin has no such item.")

    def schedule_workout(self, workout_id: int, day: date) -> JSON:
        def apply() -> JSON:
            entry_id = self.add_calendar_entry(workout_id, day)
            self._queue(workout_id, self.last_used["userDeviceId"])
            return {"workoutScheduleId": entry_id, "calendarDate": day.isoformat()}

        return self._write("schedule_workout", (workout_id, day), apply)  # type: ignore[no-any-return]

    def reschedule_workout(self, scheduled_workout_id: int, day: date) -> Any:
        def apply() -> None:
            for item in self.calendar:
                if item["id"] == scheduled_workout_id:
                    item["date"] = day.isoformat()

        return self._write("reschedule_workout", (scheduled_workout_id, day), apply)

    # -- devices ----------------------------------------------------------

    def list_devices(self) -> list[JSON]:
        return copy.deepcopy(self.devices)

    def get_last_used_device(self) -> JSON:
        return dict(self.last_used)

    def push_workout_to_device(self, workout_id: int, device_id: int) -> Any:
        def apply() -> list[JSON]:
            if self.queue_pushes:
                self._queue(workout_id, device_id)
            return [{"deviceId": device_id, "metaDataId": workout_id}]

        return self._write("push_workout_to_device", (workout_id, device_id), apply)

    def _queue(self, workout_id: int, device_id: int) -> None:
        self.messages.append(
            {
                "messageId": next(self._ids),
                "deviceId": device_id,
                "messageType": "workouts",
                "messageStatus": "new",
                "metaData": {
                    "metaDataId": workout_id,
                    "messageName": self.workouts.get(workout_id, {}).get("workoutName"),
                },
            }
        )

    def get_device_messages(self) -> Any:
        return {"messages": copy.deepcopy(self.messages)}
