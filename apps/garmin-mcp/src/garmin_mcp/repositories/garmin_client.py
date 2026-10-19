"""The only door to Garmin Connect.

`GarminRepository` is the interface the services use: a fixed, short list
of calls, raw Garmin JSON in and out. `GarminConnectRepository` implements
it with python-garminconnect; tests use an in-memory fake. Nothing above
this layer imports garminconnect, and no tool can reach a library method
that isn't listed here.

Reads raise this app's errors (`classify_read`). Writes raise only
`WriteRejected` or `WriteOutcomeUnknown` (`classify_write`) and are made
once: the library doesn't retry POST/PUT, and neither does this.
"""

from collections.abc import Callable
from datetime import date
from typing import Any, Protocol, TypeVar

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

from garmin_mcp.auth import GarminSession
from garmin_mcp.errors import NotFoundKind
from garmin_mcp.repositories.failures import (
    classify_read,
    classify_write,
    status_code,
)

T = TypeVar("T")
JSON = dict[str, Any]


class GarminRepository(Protocol):
    # Account
    def get_profile(self) -> JSON: ...

    # Activities
    def list_activities(
        self, start: date, end: date, activity_type: str | None
    ) -> list[JSON]: ...
    def get_activity(self, activity_id: int) -> JSON: ...
    def get_activity_splits(self, activity_id: int) -> JSON: ...
    def get_activity_exercise_sets(self, activity_id: int) -> JSON: ...

    # Health and training metrics, one day each
    def get_daily_summary(self, day: date) -> JSON | None: ...
    def get_sleep(self, day: date) -> JSON | None: ...
    def get_hrv(self, day: date) -> JSON | None: ...
    def get_training_readiness(self, day: date) -> Any: ...
    def get_training_status(self, day: date) -> JSON | None: ...
    def get_max_metrics(self, day: date) -> Any: ...

    # Workouts
    def list_workouts(self, limit: int, sport_keys: list[str] | None) -> list[JSON]: ...
    def get_workout(self, workout_id: int) -> JSON: ...
    def create_workout(self, payload: JSON) -> JSON: ...
    def update_workout(self, workout_id: int, payload: JSON) -> JSON: ...

    # Calendar
    def get_calendar_month(self, year: int, month: int) -> JSON: ...
    def get_scheduled_workout(self, scheduled_workout_id: int) -> JSON: ...
    def schedule_workout(self, workout_id: int, day: date) -> JSON: ...
    def reschedule_workout(self, scheduled_workout_id: int, day: date) -> Any: ...

    # Devices
    def list_devices(self) -> list[JSON]: ...
    def get_last_used_device(self) -> JSON: ...
    def push_workout_to_device(self, workout_id: int, device_id: int) -> Any: ...
    def get_device_messages(self) -> Any: ...


class GarminConnectRepository:
    def __init__(self, session: GarminSession) -> None:
        self._session = session

    # -- plumbing ---------------------------------------------------------

    def _read(self, call: Callable[[Garmin], T], kind: NotFoundKind | None = None) -> T:
        garmin = self._session.client()
        try:
            return call(garmin)
        except Exception as exc:
            self._drop_session_if_rejected(exc)
            raise classify_read(exc, kind) from exc

    def _read_optional(self, call: Callable[[Garmin], T]) -> T | None:
        """A metric read where "Garmin has nothing for that day" is an
        answer, not an error."""
        garmin = self._session.client()
        try:
            return call(garmin)
        except GarminConnectConnectionError as exc:
            if status_code(exc) in (204, 404):
                return None
            self._drop_session_if_rejected(exc)
            raise classify_read(exc, None) from exc
        except Exception as exc:
            self._drop_session_if_rejected(exc)
            raise classify_read(exc, None) from exc

    def _write(self, call: Callable[[Garmin], T]) -> T:
        garmin = self._session.client()
        try:
            return call(garmin)
        except Exception as exc:
            self._drop_session_if_rejected(exc)
            raise classify_write(exc) from exc

    def _drop_session_if_rejected(self, exc: Exception) -> None:
        if isinstance(exc, GarminConnectAuthenticationError) or status_code(exc) == 401:
            self._session.invalidate()

    # -- account ----------------------------------------------------------

    def get_profile(self) -> JSON:
        def call(g: Garmin) -> JSON:
            return {
                "display_name": g.display_name,
                "full_name": g.full_name,
                "unit_system": g.unit_system,
            }

        return self._read(call)

    # -- activities -------------------------------------------------------

    def list_activities(
        self, start: date, end: date, activity_type: str | None
    ) -> list[JSON]:
        return self._read(
            lambda g: g.get_activities_by_date(
                start.isoformat(), end.isoformat(), activity_type, sortorder="desc"
            )
        )

    def get_activity(self, activity_id: int) -> JSON:
        return self._read(lambda g: g.get_activity(str(activity_id)), "activity")

    def get_activity_splits(self, activity_id: int) -> JSON:
        return self._read(lambda g: g.get_activity_splits(str(activity_id)), "activity")

    def get_activity_exercise_sets(self, activity_id: int) -> JSON:
        return self._read(
            lambda g: g.get_activity_exercise_sets(activity_id), "activity"
        )

    # -- metrics ----------------------------------------------------------

    def get_daily_summary(self, day: date) -> JSON | None:
        return self._read_optional(lambda g: g.get_stats(day.isoformat()))

    def get_sleep(self, day: date) -> JSON | None:
        return self._read_optional(lambda g: g.get_sleep_data(day.isoformat()))

    def get_hrv(self, day: date) -> JSON | None:
        return self._read_optional(lambda g: g.get_hrv_data(day.isoformat()))

    def get_training_readiness(self, day: date) -> Any:
        return self._read_optional(lambda g: g.get_training_readiness(day.isoformat()))

    def get_training_status(self, day: date) -> JSON | None:
        return self._read_optional(lambda g: g.get_training_status(day.isoformat()))

    def get_max_metrics(self, day: date) -> Any:
        return self._read_optional(lambda g: g.get_max_metrics(day.isoformat()))

    # -- workouts ---------------------------------------------------------

    def list_workouts(self, limit: int, sport_keys: list[str] | None) -> list[JSON]:
        return self._read(
            lambda g: g.get_workouts(
                0,
                limit,
                order_by="UPDATE_DATE",
                order_seq="DESC",
                sport_type_keys=sport_keys,
            )
        )

    def get_workout(self, workout_id: int) -> JSON:
        return self._read(lambda g: g.get_workout_by_id(workout_id), "workout")

    def create_workout(self, payload: JSON) -> JSON:
        return self._write(lambda g: g.upload_workout(payload))

    def update_workout(self, workout_id: int, payload: JSON) -> JSON:
        return self._write(lambda g: g.update_workout(workout_id, payload))

    # -- calendar ---------------------------------------------------------

    def get_calendar_month(self, year: int, month: int) -> JSON:
        return self._read(lambda g: g.get_scheduled_workouts(year, month))

    def get_scheduled_workout(self, scheduled_workout_id: int) -> JSON:
        return self._read(
            lambda g: g.get_scheduled_workout_by_id(scheduled_workout_id),
            "calendar_entry",
        )

    def schedule_workout(self, workout_id: int, day: date) -> JSON:
        return self._write(lambda g: g.schedule_workout(workout_id, day.isoformat()))

    def reschedule_workout(self, scheduled_workout_id: int, day: date) -> Any:
        # The library reads the entry before the PUT; a failure there is a
        # failure before anything was sent, which `classify_write` can't
        # tell apart, so do that read here as a read.
        self.get_scheduled_workout(scheduled_workout_id)
        return self._write(
            lambda g: g.reschedule_workout(scheduled_workout_id, day.isoformat())
        )

    # -- devices ----------------------------------------------------------

    def list_devices(self) -> list[JSON]:
        return self._read(lambda g: g.get_devices())

    def get_last_used_device(self) -> JSON:
        return self._read(lambda g: g.get_device_last_used(), "device")

    def push_workout_to_device(self, workout_id: int, device_id: int) -> Any:
        # As with rescheduling, the library first reads the workout's name.
        self.get_workout(workout_id)
        return self._write(lambda g: g.push_workout_to_device(workout_id, device_id))

    def get_device_messages(self) -> Any:
        return self._read(lambda g: g.get_device_messages())
