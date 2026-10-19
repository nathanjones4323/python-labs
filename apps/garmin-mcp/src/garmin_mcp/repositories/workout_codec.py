"""Canonical workouts <-> Garmin workout JSON.

`to_garmin` builds Garmin's structure with the library's typed workout
models (`garminconnect.workout`). `from_garmin` reads Garmin's JSON back
into the canonical form, or raises `Unsupported` when the workout uses
something the canonical form can't say (multiple segments, nested repeats,
power targets, ...) rather than dropping it.

Verification compares `from_garmin(read_back)` with
`from_garmin(to_garmin(intended))`, so both sides go through the same
rounding (pace to whole seconds per km, weight to 0.1 lb).
"""

from typing import Any

from garminconnect.workout import (
    ConditionType,
    ExecutableStep,
    RepeatGroup,
    RunningWorkout,
    SportType,
    StepType,
    StrengthWorkout,
    TargetType,
    WorkoutSegment,
    create_repeat_group,
    create_strength_exercise_step,
    create_strength_rest_step,
    create_strength_timed_step,
)
from pydantic import ValidationError

from garmin_mcp.errors import Unsupported
from garmin_mcp.repositories import exercise_catalog
from garmin_mcp.schemas import workouts as w

JSON = dict[str, Any]

STRENGTH_SPORT = {
    "sportTypeId": SportType.STRENGTH_TRAINING,
    "sportTypeKey": "strength_training",
    "displayOrder": 5,
}
RUNNING_SPORT = {
    "sportTypeId": SportType.RUNNING,
    "sportTypeKey": "running",
    "displayOrder": 1,
}
SPORT_KEYS = {"strength": "strength_training", "running": "running"}

_STEP_TYPES: dict[str, tuple[int, int]] = {
    # key: (stepTypeId, displayOrder), from /workout-service/workout/types
    "warmup": (StepType.WARMUP, 1),
    "cooldown": (StepType.COOLDOWN, 2),
    "interval": (StepType.INTERVAL, 3),
    "recovery": (StepType.RECOVERY, 4),
    "rest": (StepType.REST, 5),
}
_CONDITIONS: dict[str, tuple[int, int]] = {
    "lap.button": (ConditionType.LAP_BUTTON, 1),
    "time": (ConditionType.TIME, 2),
    "distance": (ConditionType.DISTANCE, 3),
}
_TARGETS: dict[str, int] = {
    "no.target": TargetType.NO_TARGET,
    "pace.zone": TargetType.PACE_ZONE,
    "heart.rate.zone": TargetType.HEART_RATE_ZONE,
    "cadence": TargetType.CADENCE,
}
POUND_KG = 0.45359237
WEIGHT_UNIT_POUND = {"unitId": 9, "unitKey": "pound", "factor": 453.59237}
_KIND_STEP = {"exercise": "interval", "warmup": "warmup", "cooldown": "cooldown"}
_STEP_KIND = {v: k for k, v in _KIND_STEP.items()}
# Rough paces for a duration estimate on distance steps; Garmin only shows it.
_ESTIMATE_SECONDS_PER_METER = 0.36  # 6:00/km
_ESTIMATE_SECONDS_PER_REP = 4


# -- canonical -> Garmin ---------------------------------------------------


def to_garmin(workout: w.StrengthWorkout | w.RunningWorkout) -> JSON:
    """The upload payload. Strength exercises must already be resolved
    (`exercise_id` set); see `services.workouts.resolve_exercises`."""
    if isinstance(workout, w.StrengthWorkout):
        steps, estimate = _strength_steps(workout)
        model: StrengthWorkout | RunningWorkout = StrengthWorkout(
            workoutName=workout.name,
            description=workout.description,
            estimatedDurationInSecs=estimate,
            workoutSegments=[
                WorkoutSegment(
                    segmentOrder=1, sportType=STRENGTH_SPORT, workoutSteps=steps
                )
            ],
        )
    else:
        steps, estimate = _running_steps(workout)
        model = RunningWorkout(
            workoutName=workout.name,
            description=workout.description,
            estimatedDurationInSecs=estimate,
            workoutSegments=[
                WorkoutSegment(
                    segmentOrder=1, sportType=RUNNING_SPORT, workoutSteps=steps
                )
            ],
        )
    return model.to_dict()


def _strength_steps(
    workout: w.StrengthWorkout,
) -> tuple[list[ExecutableStep | RepeatGroup], int]:
    """Laid out the way Garmin's own editor does it: a single set as a bare
    step (then its rest, if any), several sets as a repeat of exercise +
    rest, and a circuit as one repeat holding each exercise and its rest."""
    steps: list[ExecutableStep | RepeatGroup] = []
    order = 1
    estimate = 0
    for item in workout.exercises:
        if isinstance(item, w.Circuit):
            inner: list[ExecutableStep | RepeatGroup] = []
            for ex in item.exercises:
                inner += _exercise_and_rest(ex, order + 1 + len(inner))
            steps.append(create_repeat_group(item.rounds, inner, order))
            order += 1 + len(inner)
            estimate += item.rounds * sum(_seconds(ex) for ex in item.exercises)
        elif item.sets == 1:
            pair = _exercise_and_rest(item, order)
            steps += pair
            order += len(pair)
            estimate += _seconds(item)
        else:
            inner = _exercise_and_rest(item, order + 1)
            steps.append(create_repeat_group(item.sets, inner, order))
            order += 1 + len(inner)
            estimate += item.sets * _seconds(item)
    return steps, estimate


def _seconds(ex: w.StrengthExercise | w.CircuitExercise) -> int:
    work = ex.duration_seconds or (ex.reps or 0) * _ESTIMATE_SECONDS_PER_REP
    return work + (ex.rest_seconds or 0)


def _exercise_and_rest(
    ex: w.StrengthExercise | w.CircuitExercise, order: int
) -> list[ExecutableStep | RepeatGroup]:
    if ex.exercise_id is None:
        raise ValueError(f"exercise {ex.name!r} is not resolved")
    category, exercise = ex.exercise_id.split("/", 1)
    if ex.reps is not None:
        work = create_strength_exercise_step(
            category, order, ex.reps, exercise_name=exercise, description=ex.notes
        )
    else:
        assert ex.duration_seconds is not None
        work = create_strength_timed_step(
            category,
            order,
            ex.duration_seconds,
            exercise_name=exercise,
            description=ex.notes,
        )
    step_id, display = _STEP_TYPES[_KIND_STEP[ex.kind]]
    extra: JSON = {
        "stepType": {
            "stepTypeId": step_id,
            "stepTypeKey": _KIND_STEP[ex.kind],
            "displayOrder": display,
        }
    }
    pounds = _pounds(ex)
    if pounds is not None:
        # The library's helper writes kilograms x 1000; Garmin's own editor
        # writes the weight in pounds under this unit, which is the format
        # seen on the real account, so use that.
        extra |= {"weightValue": pounds, "weightUnit": dict(WEIGHT_UNIT_POUND)}
    out: list[ExecutableStep | RepeatGroup] = [work.model_copy(update=extra)]
    if ex.rest_seconds:
        out.append(create_strength_rest_step(ex.rest_seconds, order + 1))
    return out


def _pounds(ex: w.StrengthExercise | w.CircuitExercise) -> float | None:
    if ex.weight_lb is not None:
        return ex.weight_lb
    if ex.weight_kg is not None:
        return ex.weight_kg / POUND_KG
    return None


def _running_steps(
    workout: w.RunningWorkout,
) -> tuple[list[ExecutableStep | RepeatGroup], int]:
    steps: list[ExecutableStep | RepeatGroup] = []
    order = 1
    estimate = 0.0
    for item in workout.steps:
        if isinstance(item, w.RepeatBlock):
            inner: list[ExecutableStep | RepeatGroup] = []
            for i, step in enumerate(item.steps, start=1):
                inner.append(_run_step(step, order + i))
            steps.append(create_repeat_group(item.repeat_count, inner, order))
            order += 1 + len(inner)
            estimate += item.repeat_count * sum(_estimate(s) for s in item.steps)
        else:
            steps.append(_run_step(item, order))
            order += 1
            estimate += _estimate(item)
    return steps, round(estimate)


def _estimate(step: w.RunStep) -> float:
    if step.duration_seconds is not None:
        return step.duration_seconds
    if step.distance_meters is not None:
        return step.distance_meters * _ESTIMATE_SECONDS_PER_METER
    return 0


def _run_step(step: w.RunStep, order: int) -> ExecutableStep:
    step_id, step_display = _STEP_TYPES[step.type]
    if step.duration_seconds is not None:
        condition, value = "time", float(step.duration_seconds)
    elif step.distance_meters is not None:
        condition, value = "distance", float(step.distance_meters)
    else:
        condition, value = "lap.button", None
    cond_id, cond_display = _CONDITIONS[condition]

    target_key = "no.target"
    one: float | None = None
    two: float | None = None
    zone: int | None = None
    match step.target:
        case w.PaceTarget() as pace:
            # Garmin keeps pace as speed in m/s, slower end first.
            fast, slow = pace.seconds_per_km()
            target_key, one, two = "pace.zone", 1000 / slow, 1000 / fast
        case w.HeartRateTarget() as hr:
            target_key, one, two = "heart.rate.zone", hr.min_bpm, hr.max_bpm
        case w.HeartRateZoneTarget() as hrz:
            target_key, zone = "heart.rate.zone", hrz.zone
        case w.CadenceTarget() as cad:
            target_key, one, two = "cadence", cad.min_spm, cad.max_spm

    return ExecutableStep(
        stepOrder=order,
        stepType={
            "stepTypeId": step_id,
            "stepTypeKey": step.type,
            "displayOrder": step_display,
        },
        endCondition={
            "conditionTypeId": cond_id,
            "conditionTypeKey": condition,
            "displayOrder": cond_display,
            "displayable": True,
        },
        endConditionValue=value,
        targetType={
            "workoutTargetTypeId": _TARGETS[target_key],
            "workoutTargetTypeKey": target_key,
            "displayOrder": 1,
        },
        targetValueOne=one,
        targetValueTwo=two,
        zoneNumber=zone,
        description=step.notes,
    )


# -- Garmin -> canonical ---------------------------------------------------


def sport_key(raw: JSON) -> str | None:
    sport = raw.get("sportType") or {}
    key = sport.get("sportTypeKey")
    return key if isinstance(key, str) else None


def from_garmin(raw: JSON) -> w.StrengthWorkout | w.RunningWorkout:
    key = sport_key(raw)
    segments = raw.get("workoutSegments") or []
    if len(segments) != 1:
        raise Unsupported(
            f"the workout has {len(segments)} segments; only one is supported"
        )
    steps = segments[0].get("workoutSteps") or []
    base: JSON = {
        "name": raw.get("workoutName") or "",
        "description": raw.get("description") or None,
    }
    try:
        if key == "strength_training":
            return w.StrengthWorkout.model_validate(
                base | {"sport": "strength", "exercises": _read_strength(steps)}
            )
        if key == "running":
            return w.RunningWorkout.model_validate(
                base | {"sport": "running", "steps": _read_running(steps)}
            )
    except ValidationError as exc:
        raise Unsupported(f"the workout doesn't fit the canonical form: {exc}") from exc
    raise Unsupported(f"sport {key!r} isn't supported; only strength and running are")


def _is_repeat(step: JSON) -> bool:
    return (
        step.get("type") == "RepeatGroupDTO"
        or _key(step, "stepType", "stepTypeKey") == "repeat"
    )


def _key(step: JSON, field: str, key: str) -> str | None:
    value = (step.get(field) or {}).get(key)
    return value if isinstance(value, str) else None


def _read_strength(steps: list[JSON]) -> list[JSON]:
    items: list[JSON] = []
    for step in steps:
        if _is_repeat(step):
            pairs = _exercise_pairs(step.get("workoutSteps") or [])
            rounds = int(step.get("numberOfIterations") or 1)
            if len(pairs) == 1:
                items.append(_read_exercise(*pairs[0]) | {"sets": rounds})
            else:
                exercises = [_read_exercise(*pair) for pair in pairs]
                items.append(
                    {"type": "circuit", "rounds": rounds, "exercises": exercises}
                )
        elif _is_rest(step):
            # Garmin's editor puts a single set's rest right after it.
            last = items[-1] if items else None
            if (
                not last
                or last.get("type") == "circuit"
                or last.get("sets") != 1
                or last.get("rest_seconds")
            ):
                raise Unsupported("a rest step that doesn't follow a single set")
            last["rest_seconds"] = _rest_seconds(step)
        else:
            items.append(_read_exercise(step, None) | {"sets": 1})
    return items


def _is_rest(step: JSON) -> bool:
    return _key(step, "stepType", "stepTypeKey") == "rest"


def _exercise_pairs(inner: list[JSON]) -> list[tuple[JSON, JSON | None]]:
    """A repeat's steps as (exercise, its rest or None)."""
    pairs: list[tuple[JSON, JSON | None]] = []
    for step in inner:
        if _is_repeat(step):
            raise Unsupported("nested repeats aren't supported")
        if not _is_rest(step):
            pairs.append((step, None))
        elif pairs and pairs[-1][1] is None:
            pairs[-1] = (pairs[-1][0], step)
        else:
            raise Unsupported(
                "a repeat has a rest step that doesn't follow an exercise"
            )
    if not pairs:
        raise Unsupported("a repeat has no exercises")
    return pairs


def _rest_seconds(rest: JSON) -> int:
    if _key(rest, "endCondition", "conditionTypeKey") != "time":
        raise Unsupported("only timed rests are supported (not lap-button rests)")
    return round(float(rest.get("endConditionValue") or 0))


def _read_exercise(step: JSON, rest: JSON | None) -> JSON:
    category = step.get("category")
    if not isinstance(category, str) or not category:
        raise Unsupported("a strength step has no exercise category")
    step_type = _key(step, "stepType", "stepTypeKey")
    kind = _STEP_KIND.get(step_type or "")
    if kind is None:
        raise Unsupported(f"strength step type {step_type!r} isn't supported")
    exercise_id = f"{category}/{step.get('exerciseName') or ''}"
    match = exercise_catalog.by_id(exercise_id)
    out: JSON = {
        "name": match.name if match else exercise_id,
        "exercise_id": exercise_id,
        "kind": kind,
        "notes": step.get("description") or None,
    }
    condition = _key(step, "endCondition", "conditionTypeKey")
    value = step.get("endConditionValue")
    if condition == "reps" and value:
        out["reps"] = round(float(value))
    elif condition == "time" and value:
        out["duration_seconds"] = round(float(value))
    else:
        raise Unsupported(
            f"{exercise_id} ends on {condition!r}; only reps or time are supported"
        )
    out |= _weight(step)
    if rest is not None:
        out["rest_seconds"] = _rest_seconds(rest)
    return out


def _weight(step: JSON) -> JSON:
    """`weightValue` is in `weightUnit` (275 under "pound" is 275 lb, as
    Garmin's editor stores it); zero or missing means no weight."""
    value = step.get("weightValue")
    if value is None or float(value) <= 0:
        return {}
    match (step.get("weightUnit") or {}).get("unitKey"):
        case "pound":
            return {"weight_lb": round(float(value), 1)}
        case "kilogram":
            # Not seen on a real account yet; read the same way as pounds.
            return {"weight_kg": round(float(value), 2)}
        case other:
            raise Unsupported(f"weight unit {other!r} isn't supported")


def _read_running(steps: list[JSON]) -> list[JSON]:
    items: list[JSON] = []
    for step in steps:
        if not _is_repeat(step):
            items.append(_read_run_step(step))
            continue
        inner = step.get("workoutSteps") or []
        if any(_is_repeat(s) for s in inner):
            raise Unsupported("nested repeats aren't supported")
        count = int(step.get("numberOfIterations") or 1)
        parsed = [_read_run_step(s) for s in inner]
        if count == 1:
            items.extend(parsed)
        else:
            items.append({"type": "repeat", "repeat_count": count, "steps": parsed})
    return items


def _read_run_step(step: JSON) -> JSON:
    kind = _key(step, "stepType", "stepTypeKey")
    if kind not in _STEP_TYPES:
        raise Unsupported(f"step type {kind!r} isn't supported")
    out: JSON = {"type": kind, "notes": step.get("description") or None}
    condition = _key(step, "endCondition", "conditionTypeKey")
    value = step.get("endConditionValue")
    if condition == "time":
        out["duration_seconds"] = round(float(value or 0))
    elif condition == "distance":
        out["distance_meters"] = round(float(value or 0), 1)
    elif condition != "lap.button":
        raise Unsupported(
            f"a {kind} step ends on {condition!r}; only time, distance or the "
            f"lap button are supported"
        )
    out["target"] = _read_target(step)
    return out


def _read_target(step: JSON) -> JSON | None:
    key = _key(step, "targetType", "workoutTargetTypeKey")
    one, two = step.get("targetValueOne"), step.get("targetValueTwo")
    zone = step.get("zoneNumber")
    if key in (None, "no.target"):
        return None
    if key == "heart.rate.zone" and zone:
        return {"type": "heart_rate_zone", "zone": int(zone)}
    if key not in ("heart.rate.zone", "cadence", "pace.zone"):
        raise Unsupported(f"target {key!r} isn't supported")
    if not one or not two:
        raise Unsupported(f"a {key} target without both limits isn't supported")
    low, high = float(one), float(two)
    if key == "heart.rate.zone":
        return {"type": "heart_rate", "min_bpm": round(low), "max_bpm": round(high)}
    if key == "cadence":
        return {"type": "cadence", "min_spm": round(low), "max_spm": round(high)}
    if key == "pace.zone":
        slow, fast = sorted((low, high))
        return {
            "type": "pace",
            "fast_seconds": round(1000 / fast),
            "slow_seconds": round(1000 / slow),
            "per": "km",
        }
    raise Unsupported(f"target {key!r} isn't supported")
