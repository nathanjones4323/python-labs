"""The Garmin calendar: reading a date range, scheduling and rescheduling.

Garmin's calendar is read a month at a time. A scheduled workout's entry
has its own id (`scheduled_workout_id`), separate from the workout's; the
same workout can be on the calendar many times.
"""

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from garmin_mcp.errors import (
    Ambiguous,
    Conflict,
    GarminApiError,
    InvalidInput,
    NotFound,
)
from garmin_mcp.repositories.garmin_client import JSON, GarminRepository
from garmin_mcp.schemas.calendar import Calendar, CalendarActivity, ScheduledWorkout
from garmin_mcp.schemas.results import ChangePreview, Verification, WriteResult
from garmin_mcp.services.writes import Checked, WritePlan, WriteRunner

MAX_RANGE_DAYS = 92
# Scheduling a year and more ahead is almost certainly a typo in the year.
MAX_DAYS_AHEAD = 366


class CalendarService:
    def __init__(
        self,
        repo: GarminRepository,
        runner: WriteRunner,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._repo = repo
        self._runner = runner
        self._today = today

    # -- reads ------------------------------------------------------------

    def get_calendar(self, start: date, end: date) -> Calendar:
        if end < start:
            raise InvalidInput("end_date is before start_date")
        if (end - start).days >= MAX_RANGE_DAYS:
            raise InvalidInput(f"ask for at most {MAX_RANGE_DAYS} days at a time")
        workouts: list[ScheduledWorkout] = []
        activities: list[CalendarActivity] = []
        for year, month in _months(start, end):
            for item in (
                self._repo.get_calendar_month(year, month).get("calendarItems") or []
            ):
                day = _parse_date(item.get("date"))
                if day is None or not start <= day <= end:
                    continue
                if item.get("itemType") == "workout" and item.get("id"):
                    workouts.append(_scheduled(item, day))
                elif item.get("itemType") == "activity" and item.get("id"):
                    activities.append(
                        CalendarActivity(
                            activity_id=int(item["id"]),
                            date=day,
                            title=item.get("title"),
                            activity_type=item.get("activityTypeKey"),
                        )
                    )
        workouts.sort(key=lambda e: (e.date, e.scheduled_workout_id))
        activities.sort(key=lambda a: (a.date, a.activity_id))
        return Calendar(
            start_date=start,
            end_date=end,
            scheduled_workouts=workouts,
            activities=activities,
        )

    def entries_on(self, day: date) -> list[ScheduledWorkout]:
        return self.get_calendar(day, day).scheduled_workouts

    # -- writes -----------------------------------------------------------

    def schedule_workout(
        self,
        workout_id: int,
        day: date,
        *,
        allow_duplicate: bool,
        dry_run: bool,
        confirmation_token: str | None,
    ) -> WriteResult:
        self._check_future(day, "date")
        workout = self._repo.get_workout(workout_id)
        name = workout.get("workoutName") or f"workout {workout_id}"
        existing = self.entries_on(day)
        warnings = self._conflicts(existing, workout_id, day, allow_duplicate)
        before = sorted(e.scheduled_workout_id for e in existing)

        def find_new() -> Checked:
            new = [
                e
                for e in self.entries_on(day)
                if e.workout_id == workout_id and e.scheduled_workout_id not in before
            ]
            checked = f"the calendar on {day}"
            if len(new) == 1:
                return Verification(status="verified", checked=checked), {
                    "scheduled_workout_id": new[0].scheduled_workout_id,
                    "workout_id": workout_id,
                    "date": day.isoformat(),
                }
            return Verification(
                status="not_found" if not new else "mismatch",
                checked=checked,
                mismatches=[f"{len(new)} new entries for workout {workout_id}"],
            ), {}

        plan = WritePlan(
            operation="schedule_workout",
            preview=ChangePreview(
                summary=(
                    f"Schedule {name!r} (workout {workout_id}) on {day:%a %Y-%m-%d}"
                ),
                details=[f"{e.date}: already has {e.title!r}" for e in existing],
                warnings=warnings,
            ),
            basis={"workout_id": workout_id, "date": day, "before": before},
            execute=lambda: self._repo.schedule_workout(workout_id, day),
            verify=lambda _response: find_new(),
            reconcile=find_new,
            refs={"workout_id": workout_id, "date": day},
        )
        return self._runner.run(
            plan, dry_run=dry_run, confirmation_token=confirmation_token
        )

    def reschedule_workout(
        self,
        to_date: date,
        *,
        scheduled_workout_id: int | None,
        workout_id: int | None,
        from_date: date | None,
        allow_duplicate: bool,
        dry_run: bool,
        confirmation_token: str | None,
    ) -> WriteResult:
        entry = self._find_entry(scheduled_workout_id, workout_id, from_date)
        self._check_future(to_date, "to_date")
        if entry.date == to_date:
            raise InvalidInput(
                f"entry {entry.scheduled_workout_id} is already on {to_date}"
            )
        target = self.entries_on(to_date)
        warnings = self._conflicts(target, entry.workout_id, to_date, allow_duplicate)
        entry_id = entry.scheduled_workout_id

        def check() -> Checked:
            moved = _scheduled_from_detail(
                self._repo.get_scheduled_workout(entry_id), entry_id
            )
            ids = {"scheduled_workout_id": entry_id, "to_date": to_date.isoformat()}
            checked = f"calendar entry {entry_id} and the calendar on {to_date}"
            mismatches = []
            if moved.date != to_date:
                mismatches.append(f"entry {entry_id} is on {moved.date}, not {to_date}")
            if not any(
                e.scheduled_workout_id == entry_id for e in self.entries_on(to_date)
            ):
                mismatches.append(
                    f"the calendar on {to_date} doesn't list entry {entry_id}"
                )
            return Verification(
                status="mismatch" if mismatches else "verified",
                checked=checked,
                mismatches=mismatches,
            ), ids

        plan = WritePlan(
            operation="reschedule_workout",
            preview=ChangePreview(
                summary=(
                    f"Move {entry.title or 'workout'!r} (entry {entry_id}) from "
                    f"{entry.date:%a %Y-%m-%d} to {to_date:%a %Y-%m-%d}"
                ),
                details=[f"{e.date}: already has {e.title!r}" for e in target],
                warnings=warnings,
            ),
            basis={
                "entry": entry_id,
                "from": entry.date,
                "to": to_date,
                "target": sorted(e.scheduled_workout_id for e in target),
            },
            execute=lambda: self._repo.reschedule_workout(entry_id, to_date),
            verify=lambda _response: check(),
            reconcile=check,
            refs={
                "scheduled_workout_id": entry_id,
                "from_date": entry.date,
                "to_date": to_date,
            },
        )
        return self._runner.run(
            plan, dry_run=dry_run, confirmation_token=confirmation_token
        )

    # -- helpers ----------------------------------------------------------

    def _find_entry(
        self,
        scheduled_workout_id: int | None,
        workout_id: int | None,
        from_date: date | None,
    ) -> ScheduledWorkout:
        if scheduled_workout_id is not None:
            if workout_id is not None or from_date is not None:
                raise InvalidInput(
                    "give scheduled_workout_id alone, or workout_id with "
                    "from_date, not both"
                )
            detail = self._repo.get_scheduled_workout(scheduled_workout_id)
            return _scheduled_from_detail(detail, scheduled_workout_id)
        if workout_id is None or from_date is None:
            raise InvalidInput(
                "give scheduled_workout_id, or both workout_id and from_date"
            )
        matches = [e for e in self.entries_on(from_date) if e.workout_id == workout_id]
        if not matches:
            raise NotFound(
                "calendar_entry", f"workout {workout_id} isn't scheduled on {from_date}"
            )
        if len(matches) > 1:
            ids = ", ".join(str(e.scheduled_workout_id) for e in matches)
            raise Ambiguous(
                f"workout {workout_id} is on {from_date} {len(matches)} times "
                f"(entries {ids}); pass scheduled_workout_id"
            )
        return matches[0]

    def _check_future(self, day: date, field: str) -> None:
        today = self._today()
        if day < today:
            raise InvalidInput(f"{field} {day} is in the past (today is {today})")
        if day > today + timedelta(days=MAX_DAYS_AHEAD):
            raise InvalidInput(
                f"{field} {day} is more than a year ahead; check the year"
            )

    @staticmethod
    def _conflicts(
        existing: list[ScheduledWorkout],
        workout_id: int | None,
        day: date,
        allow_duplicate: bool,
    ) -> list[str]:
        same = [e for e in existing if e.workout_id == workout_id]
        if same and not allow_duplicate:
            raise Conflict(
                f"workout {workout_id} is already scheduled on {day} (entry "
                f"{same[0].scheduled_workout_id}). Pass allow_duplicate=true "
                f"to add it a second time."
            )
        warnings = [
            f"{day} already has {e.title!r} (workout {e.workout_id})"
            for e in existing
            if e not in same
        ]
        if same:
            warnings.append(
                f"workout {workout_id} will be on {day} {len(same) + 1} times"
            )
        return warnings


def _months(start: date, end: date) -> list[tuple[int, int]]:
    months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _scheduled(item: JSON, day: date) -> ScheduledWorkout:
    workout_id = item.get("workoutId")
    return ScheduledWorkout(
        scheduled_workout_id=int(item["id"]),
        workout_id=int(workout_id) if workout_id else None,
        date=day,
        title=item.get("title"),
        sport=item.get("sportTypeKey"),
    )


def _scheduled_from_detail(detail: JSON, scheduled_workout_id: int) -> ScheduledWorkout:
    """`GET /workout-service/schedule/{id}` has a different shape from the
    month view: the date is `calendarDate` and the workout is nested."""
    day = _parse_date(detail.get("calendarDate") or detail.get("date"))
    if day is None:
        raise GarminApiError(
            f"calendar entry {scheduled_workout_id} came back without a date"
        )
    workout = detail.get("workout") or {}
    workout_id = workout.get("workoutId") or detail.get("workoutId")
    sport = (workout.get("sportType") or {}).get("sportTypeKey")
    return ScheduledWorkout(
        scheduled_workout_id=scheduled_workout_id,
        workout_id=int(workout_id) if workout_id else None,
        date=day,
        title=workout.get("workoutName") or detail.get("title"),
        sport=sport,
    )
