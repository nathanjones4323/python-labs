"""Live tests against the real Garmin Connect account. Opt-in, and skipped
otherwise:

    GARMIN_LIVE_TESTS=1                     reads only
    GARMIN_LIVE_TESTS=1 GARMIN_LIVE_WRITES=1   also creates, updates,
                                               schedules and reschedules
                                               test workouts
    GARMIN_LIVE_DEVICE_ID=<id>              also pushes one to that device

Run them with `uv run --package garmin-mcp pytest -m live -s`. They use the
token store from `garmin-mcp-login` (GARMIN_TOKENSTORE or the default).

Everything written is named "[garmin-mcp test] ..." and scheduled 30+ days
out, and is removed afterwards (calendar entries first, then workouts),
using the library directly: deletion isn't a tool.

The account's own workouts are never written to. Updates only ever target a
workout this run created (and whose name carries the prefix), and
`test_real_workouts_untouched`, which runs last, compares every other
workout with a snapshot taken before the first write.
"""

import os
import time
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from garmin_mcp.config import Settings
from garmin_mcp.schemas.results import WriteResult
from garmin_mcp.schemas.workouts import RunningWorkout, StrengthWorkout, Workout
from garmin_mcp.server import App, account_status, build_app
from pydantic import TypeAdapter

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("GARMIN_LIVE_TESTS") != "1", reason="set GARMIN_LIVE_TESTS=1"
    ),
]
writes = pytest.mark.skipif(
    os.environ.get("GARMIN_LIVE_WRITES") != "1", reason="set GARMIN_LIVE_WRITES=1"
)
PREFIX = "[garmin-mcp test]"
workout: TypeAdapter[StrengthWorkout | RunningWorkout] = TypeAdapter(Workout)


class Created:
    def __init__(self) -> None:
        self.workouts: list[int] = []
        self.entries: list[int] = []
        # The account's own workouts, before any write: id -> full JSON.
        self.real_before: dict[int, dict[str, Any]] = {}


def real_workouts(app: App) -> dict[int, dict[str, Any]]:
    """Every workout not made by these tests, read in full."""
    out = {}
    for summary in app.repo.list_workouts(100, None):
        if not (summary.get("workoutName") or "").startswith(PREFIX):
            workout_id = int(summary["workoutId"])
            out[workout_id] = app.repo.get_workout(workout_id)
    return out


@pytest.fixture(scope="module")
def live() -> Iterator[tuple[App, Created]]:
    # The real environment, not the unit tests' isolated one.
    settings = Settings(dry_run=False)
    app = build_app(settings)
    created = Created()
    created.real_before = real_workouts(app)
    yield app, created
    if app.session is None:
        return
    garmin = app.session.client()
    for entry in created.entries:
        try:
            garmin.unschedule_workout(entry)
        except Exception as exc:  # noqa: BLE001 - report and keep cleaning
            print(f"couldn't unschedule {entry}: {exc}")
    for workout_id in created.workouts:
        try:
            garmin.delete_workout(workout_id)
        except Exception as exc:  # noqa: BLE001
            print(f"couldn't delete workout {workout_id}: {exc}")


@pytest.fixture(autouse=True)
def _no_env() -> None:
    # Overrides conftest's fixture of the same name, which hides GARMIN_*
    # settings and `.env`: these tests need the real ones.
    return None


def approve(call: Any, *args: Any, **kwargs: Any) -> WriteResult:
    preview = call(*args, confirmation_token=None, **kwargs)
    assert preview.status == "approval_required", preview
    result: WriteResult = call(
        *args, confirmation_token=preview.confirmation_token, **kwargs
    )
    return result


def name(kind: str) -> str:
    return f"{PREFIX} {kind} {int(time.time())}"


# -- reads -----------------------------------------------------------------


def test_authenticated(live: tuple[App, Created]) -> None:
    status = account_status(live[0])
    assert status.authenticated, status.error


def test_reads(live: tuple[App, Created]) -> None:
    app = live[0]
    today = date.today()
    app.activities.recent(today - timedelta(days=30), today, None, 10)
    app.workouts.list_workouts(None, 10)
    app.calendar.get_calendar(today, today + timedelta(days=14))
    app.health.metrics(today - timedelta(days=1), None, ["daily_summary", "sleep"])
    app.devices.list_devices()


# -- writes ----------------------------------------------------------------


@writes
def test_strength_create_read_back(live: tuple[App, Created]) -> None:
    app, created = live
    spec = workout.validate_python(
        {
            "name": name("strength"),
            "sport": "strength",
            "exercises": [
                {
                    "name": "Barbell Back Squat",
                    "sets": 3,
                    "reps": 5,
                    "weight_kg": 80,
                    "rest_seconds": 120,
                },
                {
                    "name": "Plank",
                    "sets": 2,
                    "duration_seconds": 45,
                    "rest_seconds": 30,
                },
            ],
        }
    )
    result = approve(
        app.workouts.create_workout, spec, allow_duplicate_name=False, dry_run=False
    )
    if result.result:
        created.workouts.append(result.result["workout_id"])
    assert result.status == "verified", result.verification


@writes
def test_running_create_schedule_reschedule(live: tuple[App, Created]) -> None:
    app, created = live
    spec = workout.validate_python(
        {
            "name": name("intervals"),
            "sport": "running",
            "steps": [
                {"type": "warmup", "duration_seconds": 600},
                {
                    "type": "repeat",
                    "repeat_count": 4,
                    "steps": [
                        {
                            "type": "interval",
                            "distance_meters": 800,
                            "target": {
                                "type": "pace",
                                "fast_seconds": 255,
                                "slow_seconds": 270,
                            },
                        },
                        {"type": "recovery", "duration_seconds": 120},
                    ],
                },
                {"type": "cooldown"},
            ],
        }
    )
    result = approve(
        app.workouts.create_workout, spec, allow_duplicate_name=False, dry_run=False
    )
    assert result.result, result
    workout_id = result.result["workout_id"]
    created.workouts.append(workout_id)
    assert result.status == "verified", result.verification

    day = date.today() + timedelta(days=30)
    scheduled = approve(
        app.calendar.schedule_workout,
        workout_id,
        day,
        allow_duplicate=False,
        dry_run=False,
    )
    assert scheduled.result, scheduled
    entry = scheduled.result["scheduled_workout_id"]
    created.entries.append(entry)
    assert scheduled.status == "verified", scheduled.verification

    moved = approve(
        app.calendar.reschedule_workout,
        day + timedelta(days=1),
        scheduled_workout_id=entry,
        workout_id=None,
        from_date=None,
        allow_duplicate=False,
        dry_run=False,
    )
    assert moved.status == "verified", moved.verification

    device = os.environ.get("GARMIN_LIVE_DEVICE_ID")
    if device:
        pushed = approve(
            app.devices.push_workout, workout_id, int(device), dry_run=False
        )
        # Upload and sync are separate: queued is what can be checked here;
        # whether the watch took it shows on the watch after its next sync.
        assert pushed.result and pushed.result["upload_status"] == "accepted", pushed
        print(f"push: {pushed.result}")


@writes
def test_update_a_test_workout(live: tuple[App, Created]) -> None:
    """Create a workout laid out like the ones Garmin's editor makes (bare
    warm-ups, then a circuit, weights in pounds), schedule it, update it,
    and check the update landed and the calendar entry still points at it."""
    app, created = live
    spec = workout.validate_python(
        {
            "name": name("update"),
            "sport": "strength",
            "exercises": [
                {"name": "Air Squat", "kind": "warmup", "sets": 1, "reps": 15},
                {"name": "Arm Circles", "kind": "warmup", "sets": 1, "reps": 10},
                {
                    "type": "circuit",
                    "rounds": 5,
                    "exercises": [
                        {
                            "name": "Barbell Back Squat",
                            "reps": 5,
                            "weight_lb": 135,
                            "rest_seconds": 180,
                        },
                        {
                            "name": "Barbell Bench Press",
                            "reps": 5,
                            "weight_lb": 95,
                            "rest_seconds": 180,
                        },
                    ],
                },
            ],
        }
    )
    result = approve(
        app.workouts.create_workout, spec, allow_duplicate_name=False, dry_run=False
    )
    assert result.result, result
    workout_id = result.result["workout_id"]
    created.workouts.append(workout_id)
    assert result.status == "verified", result.verification

    day = date.today() + timedelta(days=32)
    scheduled = approve(
        app.calendar.schedule_workout,
        workout_id,
        day,
        allow_duplicate=False,
        dry_run=False,
    )
    assert scheduled.result, scheduled
    entry = scheduled.result["scheduled_workout_id"]
    created.entries.append(entry)
    assert scheduled.status == "verified", scheduled.verification

    # Only ever update what this run made.
    current = app.workouts.get_workout(workout_id)
    assert workout_id in created.workouts
    assert workout_id not in created.real_before
    assert current.name.startswith(PREFIX)
    assert current.definition is not None, current.unsupported_reason

    edited = current.definition.model_dump()
    circuit = edited["exercises"][2]
    circuit["rounds"] = 3
    circuit["exercises"][0]["weight_lb"] = 145
    circuit["exercises"][1]["rest_seconds"] = 120
    edited["exercises"].append(
        {"name": "Plank", "kind": "cooldown", "sets": 1, "duration_seconds": 60}
    )
    updated = approve(
        app.workouts.update_workout,
        workout_id,
        workout.validate_python(edited),
        dry_run=False,
    )
    print(f"update preview: {updated.preview.details}")
    assert updated.status == "verified", updated.verification

    after = app.workouts.get_workout(workout_id)
    assert after.workout_id == workout_id
    assert after.definition is not None
    # Compared with the edit as the server resolved it (the added Plank gets
    # its catalog id).
    intended = app.workouts.resolve_exercises(workout.validate_python(edited))
    assert after.definition == intended
    assert isinstance(after.definition, StrengthWorkout)
    assert getattr(after.definition.exercises[2], "rounds", None) == 3

    on_day = app.calendar.get_calendar(day, day).scheduled_workouts
    assert [
        (e.scheduled_workout_id, e.workout_id)
        for e in on_day
        if e.scheduled_workout_id == entry
    ] == [(entry, workout_id)]


def test_real_workouts_untouched(live: tuple[App, Created]) -> None:
    """Runs last: every workout that isn't a test workout is exactly as it
    was before the first write."""
    app, created = live
    assert real_workouts(app) == created.real_before
