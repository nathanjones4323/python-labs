"""Canonical schema validation and the Garmin codec, with no Garmin at all."""

from typing import Any

import pytest
from garmin_mcp.errors import Unsupported
from garmin_mcp.repositories.workout_codec import from_garmin, to_garmin
from garmin_mcp.schemas.workouts import (
    Circuit,
    PaceTarget,
    RunningWorkout,
    StrengthExercise,
    StrengthWorkout,
    Workout,
)
from pydantic import TypeAdapter, ValidationError

from tests.conftest import running_spec, strength_spec

workout: TypeAdapter[StrengthWorkout | RunningWorkout] = TypeAdapter(Workout)


def resolved_strength(**overrides: Any) -> StrengthWorkout:
    spec = strength_spec(**overrides)
    ids = {
        "Barbell Bench Press": "BENCH_PRESS/BARBELL_BENCH_PRESS",
        "push up": "PUSH_UP/PUSH_UP",
        "Plank": "PLANK/PLANK",
    }
    names = {"push up": "Push-up"}
    for ex in spec["exercises"]:
        ex["exercise_id"] = ids[ex["name"]]
        ex["name"] = names.get(ex["name"], ex["name"])
    return StrengthWorkout.model_validate(spec)


# -- schemas ---------------------------------------------------------------


def test_valid_workouts_parse() -> None:
    assert isinstance(workout.validate_python(strength_spec()), StrengthWorkout)
    assert isinstance(workout.validate_python(running_spec()), RunningWorkout)


@pytest.mark.parametrize(
    "change",
    [
        {"sport": "cycling"},
        {"name": ""},
        {"exercises": []},
        {"surprise": 1},
    ],
)
def test_rejects_bad_workouts(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        workout.validate_python(strength_spec(**change))


@pytest.mark.parametrize(
    "exercise",
    [
        {"name": "Plank", "sets": 0, "reps": 5},
        {"name": "Plank", "sets": 3, "reps": 0},
        {"name": "Plank", "sets": 3, "reps": -1},
        {"name": "Plank", "sets": 3},
        {"name": "Plank", "sets": 3, "reps": 5, "duration_seconds": 30},
        {"name": "Plank", "sets": 3, "reps": 5, "weight_kg": -5},
        {"name": "Plank", "sets": 3, "reps": 5, "exercise_id": "plank"},
    ],
)
def test_rejects_bad_exercises(exercise: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        StrengthExercise.model_validate(exercise)


def test_zero_rest_is_no_rest() -> None:
    ex = StrengthExercise(name="Plank", sets=3, duration_seconds=30, rest_seconds=0)
    assert ex.rest_seconds is None


@pytest.mark.parametrize(
    "step",
    [
        {"type": "sprint", "duration_seconds": 30},
        {"type": "interval", "duration_seconds": 0},
        {"type": "interval", "distance_meters": -400},
        {"type": "interval", "duration_seconds": 60, "distance_meters": 400},
        {
            "type": "rest",
            "duration_seconds": 60,
            "target": {"type": "heart_rate_zone", "zone": 1},
        },
        {"type": "interval", "target": {"type": "heart_rate_zone", "zone": 6}},
        {
            "type": "interval",
            "target": {"type": "heart_rate", "min_bpm": 170, "max_bpm": 150},
        },
        {"type": "interval", "target": {"type": "power", "min": 200, "max": 250}},
    ],
)
def test_rejects_bad_steps(step: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        workout.validate_python(running_spec(steps=[step]))


def test_pace_ranges() -> None:
    with pytest.raises(ValidationError, match="fast_seconds must be <="):
        PaceTarget(type="pace", fast_seconds=300, slow_seconds=280)
    with pytest.raises(ValidationError, match="outside"):
        PaceTarget(type="pace", fast_seconds=60, slow_seconds=90)
    # 8:00/mi is 4:58/km.
    mile = PaceTarget(type="pace", fast_seconds=480, slow_seconds=480, per="mi")
    assert round(mile.seconds_per_km()[0]) == 298


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([{"type": "interval"}, {"type": "warmup"}], "warmup"),
        ([{"type": "cooldown"}, {"type": "interval"}], "cooldown"),
        ([{"type": "warmup"}, {"type": "cooldown"}, {"type": "warmup"}], "warmup"),
    ],
)
def test_step_order(steps: list[dict[str, Any]], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        workout.validate_python(running_spec(steps=steps))


def test_warmup_and_cooldown_only_is_fine() -> None:
    workout.validate_python(
        running_spec(steps=[{"type": "warmup"}, {"type": "cooldown"}])
    )


@pytest.mark.parametrize(
    "block",
    [
        {"type": "repeat", "repeat_count": 1, "steps": [{"type": "interval"}]},
        {"type": "repeat", "repeat_count": 5, "steps": []},
        {"type": "repeat", "repeat_count": 5, "steps": [{"type": "warmup"}]},
        {
            "type": "repeat",
            "repeat_count": 5,
            "steps": [{"type": "repeat", "repeat_count": 2, "steps": []}],
        },
    ],
)
def test_rejects_bad_repeats(block: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        workout.validate_python(running_spec(steps=[block]))


def test_rejects_too_many_steps() -> None:
    block = {"type": "repeat", "repeat_count": 2, "steps": [{"type": "interval"}] * 10}
    with pytest.raises(ValidationError, match="limit"):
        workout.validate_python(running_spec(steps=[block] * 5))


# -- codec -----------------------------------------------------------------


def test_strength_round_trip() -> None:
    spec = resolved_strength()
    payload = to_garmin(spec)
    assert payload["sportType"]["sportTypeKey"] == "strength_training"
    bench = payload["workoutSegments"][0]["workoutSteps"][0]
    assert bench["numberOfIterations"] == 4
    exercise, rest = bench["workoutSteps"]
    assert (exercise["category"], exercise["exerciseName"]) == (
        "BENCH_PRESS",
        "BARBELL_BENCH_PRESS",
    )
    # In pounds, as Garmin's own editor stores it.
    assert exercise["weightValue"] == 135
    assert exercise["weightUnit"]["unitKey"] == "pound"
    assert rest["endConditionValue"] == 90
    # A single set without rest is a bare step, as the editor makes it.
    assert from_garmin(payload) == spec


def test_step_orders_are_unique() -> None:
    payload = to_garmin(resolved_strength())

    def orders(steps: list[dict[str, Any]]) -> list[int]:
        out = []
        for s in steps:
            out.append(s["stepOrder"])
            out += orders(s.get("workoutSteps") or [])
        return out

    found = orders(payload["workoutSegments"][0]["workoutSteps"])
    assert len(found) == len(set(found))


def test_running_round_trip() -> None:
    spec = RunningWorkout.model_validate(running_spec())
    payload = to_garmin(spec)
    repeat = payload["workoutSegments"][0]["workoutSteps"][1]
    interval = repeat["workoutSteps"][0]
    assert interval["endCondition"]["conditionTypeKey"] == "distance"
    # Pace as speed in m/s, slower end first.
    assert interval["targetValueOne"] == pytest.approx(1000 / 240)
    assert interval["targetValueTwo"] == pytest.approx(1000 / 230)
    cooldown = payload["workoutSegments"][0]["workoutSteps"][2]
    assert cooldown["endCondition"]["conditionTypeKey"] == "lap.button"
    assert from_garmin(payload) == spec


def test_mile_pace_reads_back_per_km() -> None:
    spec = RunningWorkout.model_validate(
        running_spec(
            steps=[
                {
                    "type": "interval",
                    "duration_seconds": 600,
                    "target": {
                        "type": "pace",
                        "fast_seconds": 450,
                        "slow_seconds": 480,
                        "per": "mi",
                    },
                }
            ]
        )
    )
    back = from_garmin(to_garmin(spec))
    assert isinstance(back, RunningWorkout)
    target = back.steps[0].target  # type: ignore[union-attr]
    assert target == PaceTarget(
        type="pace", fast_seconds=280, slow_seconds=298, per="km"
    )


def _payload() -> dict[str, Any]:
    return to_garmin(RunningWorkout.model_validate(running_spec()))


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda p: p["sportType"].update(sportTypeKey="cycling"), "cycling"),
        (lambda p: p["workoutSegments"].append(p["workoutSegments"][0]), "segments"),
        (
            lambda p: p["workoutSegments"][0]["workoutSteps"][0]["targetType"].update(
                workoutTargetTypeKey="power.zone"
            ),
            "power.zone",
        ),
        (
            lambda p: p["workoutSegments"][0]["workoutSteps"][1]["workoutSteps"].append(
                {"type": "RepeatGroupDTO", "workoutSteps": []}
            ),
            "nested",
        ),
    ],
)
def test_unrepresentable_workouts_are_refused(mutate: Any, reason: str) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises(Unsupported, match=reason):
        from_garmin(payload)


def test_kilograms_are_written_as_pounds() -> None:
    spec = resolved_strength()
    first = spec.exercises[0]
    assert isinstance(first, StrengthExercise)
    spec.exercises[0] = first.model_copy(update={"weight_lb": None, "weight_kg": 60})
    payload = to_garmin(spec)
    step = payload["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]
    assert step["weightValue"] == pytest.approx(132.277, abs=0.001)
    back = from_garmin(payload)
    assert isinstance(back, StrengthWorkout)
    assert back.all_exercises()[0].weight_lb == 132.3


# The shape of a strength workout made in Garmin Connect's editor, as read
# from a real account: bare warm-up steps, then one repeat holding four
# exercises each followed by a rest, weights in pounds.
def _editor_step(
    order: int, kind: str, category: str, name: str, reps: int, lb: float | None
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": 3 if kind == "interval" else 1, "stepTypeKey": kind},
        "endCondition": {"conditionTypeId": 10, "conditionTypeKey": "reps"},
        "endConditionValue": float(reps),
        "endConditionCompare": "gt",
        "category": category,
        "exerciseName": name,
        "weightUnit": {"unitId": 9, "unitKey": "pound", "factor": 453.59237},
        "description": "",
    }
    if lb is not None:
        step["weightValue"] = lb
    return step


def _editor_rest(order: int, seconds: float) -> dict[str, Any]:
    return {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": 5, "stepTypeKey": "rest"},
        "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
        "endConditionValue": seconds,
        # Seen on the real account: the editor leaves a target on rests.
        "targetType": {"workoutTargetTypeId": 9, "workoutTargetTypeKey": "power.lap"},
        "weightUnit": {"unitId": 9, "unitKey": "pound", "factor": 453.59237},
    }


EDITOR_WORKOUT: dict[str, Any] = {
    "workoutId": 1734341735,
    "workoutName": "5x5 A",
    "sportType": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
    "workoutSegments": [
        {
            "segmentOrder": 1,
            "workoutSteps": [
                _editor_step(1, "warmup", "SQUAT", "AIR_SQUAT", 15, 0.0),
                _editor_step(2, "warmup", "BANDED_EXERCISES", "PULL_APART", 15, 0.0),
                _editor_step(3, "warmup", "PULL_UP", "PULL_UP", 3, None),
                {
                    "type": "RepeatGroupDTO",
                    "stepOrder": 4,
                    "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
                    "numberOfIterations": 5,
                    "skipLastRestStep": False,
                    "smartRepeat": False,
                    "workoutSteps": [
                        _editor_step(
                            5,
                            "interval",
                            "SQUAT",
                            "BARBELL_BACK_SQUAT",
                            5,
                            274.99801198155075,
                        ),
                        _editor_rest(6, 180.0),
                        _editor_step(
                            7,
                            "interval",
                            "BENCH_PRESS",
                            "BARBELL_BENCH_PRESS",
                            5,
                            194.9988709025242,
                        ),
                        _editor_rest(8, 180.0),
                        _editor_step(
                            9,
                            "interval",
                            "PULL_UP",
                            "WEIGHTED_PULL_UP",
                            5,
                            44.99855233455536,
                        ),
                        _editor_rest(10, 180.0),
                    ],
                },
            ],
        }
    ],
}


def test_reads_an_editor_made_circuit() -> None:
    back = from_garmin(EDITOR_WORKOUT)
    assert isinstance(back, StrengthWorkout)
    warmups = back.exercises[:3]
    assert [(e.kind, e.sets, e.reps) for e in warmups] == [  # type: ignore[union-attr]
        ("warmup", 1, 15),
        ("warmup", 1, 15),
        ("warmup", 1, 3),
    ]
    assert all(e.weight_lb is None for e in warmups)  # type: ignore[union-attr]
    circuit = back.exercises[3]
    assert isinstance(circuit, Circuit)
    assert circuit.rounds == 5
    assert [
        (e.name, e.reps, e.weight_lb, e.rest_seconds) for e in circuit.exercises
    ] == [
        ("Barbell Back Squat", 5, 275.0, 180),
        ("Barbell Bench Press", 5, 195.0, 180),
        ("Weighted Pull-up", 5, 45.0, 180),
    ]


def test_editor_circuit_survives_a_rewrite() -> None:
    """What update_workout relies on: re-encoding what was read gives the
    same workout back."""
    back = from_garmin(EDITOR_WORKOUT)
    payload = to_garmin(back)
    assert from_garmin(payload) == back
    steps = payload["workoutSegments"][0]["workoutSteps"]
    assert [s["stepType"]["stepTypeKey"] for s in steps] == [
        "warmup",
        "warmup",
        "warmup",
        "repeat",
    ]
    assert [s["stepType"]["stepTypeKey"] for s in steps[3]["workoutSteps"]] == [
        "interval",
        "rest",
    ] * 3


def test_single_set_with_rest_round_trips() -> None:
    spec = StrengthWorkout.model_validate(
        {
            "name": "One heavy single",
            "sport": "strength",
            "exercises": [
                {
                    "name": "Barbell Deadlift",
                    "exercise_id": "DEADLIFT/BARBELL_DEADLIFT",
                    "sets": 1,
                    "reps": 1,
                    "weight_lb": 405,
                    "rest_seconds": 300,
                },
                {
                    "name": "Plank",
                    "exercise_id": "PLANK/PLANK",
                    "sets": 1,
                    "duration_seconds": 60,
                    "kind": "cooldown",
                },
            ],
        }
    )
    payload = to_garmin(spec)
    kinds = [
        s["stepType"]["stepTypeKey"]
        for s in payload["workoutSegments"][0]["workoutSteps"]
    ]
    assert kinds == ["interval", "rest", "cooldown"]
    assert from_garmin(payload) == spec


@pytest.mark.parametrize(
    "item",
    [
        {"type": "circuit", "rounds": 3, "exercises": [{"name": "Plank", "reps": 5}]},
        {
            "type": "circuit",
            "rounds": 0,
            "exercises": [{"name": "Plank", "reps": 5}] * 2,
        },
        {
            "type": "circuit",
            "rounds": 3,
            "exercises": [{"name": "Plank", "reps": 5, "sets": 3}] * 2,
        },
        {"name": "Plank", "sets": 3, "reps": 5, "weight_kg": 20, "weight_lb": 45},
        {"name": "Plank", "sets": 3, "reps": 5, "kind": "stretch"},
    ],
)
def test_rejects_bad_strength_items(item: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        workout.validate_python(strength_spec(exercises=[item]))


def test_exercise_limit_counts_circuits() -> None:
    circuit = {
        "type": "circuit",
        "rounds": 2,
        "exercises": [{"name": "Plank", "reps": 5}] * 10,
    }
    with pytest.raises(ValidationError, match="counting those in circuits"):
        workout.validate_python(strength_spec(exercises=[circuit] * 5))


def test_stray_rest_is_refused() -> None:
    raw = dict(EDITOR_WORKOUT)
    steps = raw["workoutSegments"][0]["workoutSteps"]
    raw = {
        **raw,
        "workoutSegments": [{"workoutSteps": [_editor_rest(1, 60.0), *steps]}],
    }
    with pytest.raises(Unsupported, match="rest step"):
        from_garmin(raw)
