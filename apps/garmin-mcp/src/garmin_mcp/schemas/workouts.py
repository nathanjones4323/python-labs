"""Canonical workout definitions: what an agent writes and what reads return.

These are independent of Garmin's request shapes; `repositories.workout_codec`
translates both ways. Units are in the field names (`_seconds`, `_meters`,
`_kg`, `_lb`, `_bpm`) so nothing is converted implicitly.

Two deliberate departures from a flat step list:

- A running target is a typed object (`{"type": "pace", ...}`) rather than a
  `target_type` string plus one `target_value`: Garmin targets are ranges,
  and a single number can't say "5:00–5:20 per km".
- Repeats are a `repeat` block holding the steps it repeats, instead of a
  `repeat_count` on a step, since an interval and its recovery repeat
  together.
"""

from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_validator,
    model_validator,
)

# Beyond these the workout won't fit comfortably on a watch, and an agent
# that produced one has probably made a mistake.
MAX_STRENGTH_EXERCISES = 40
MAX_RUN_STEPS = 50  # counting the steps inside repeat blocks
MAX_REPEAT_STEPS = 10

METERS_PER_MILE = 1609.344

# Plausible bounds, per kilometre. 2:00/km is faster than the world record
# for a mile; 20:00/km is a slow walk.
MIN_PACE_SECONDS_PER_KM = 120
MAX_PACE_SECONDS_PER_KM = 1200

ExerciseId = Annotated[
    str,
    Field(
        pattern=r"^[A-Z0-9_]+/[A-Z0-9_]*$",
        description="CATEGORY/EXERCISE, as returned by search_exercises.",
    ),
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


ExerciseKind = Literal["exercise", "warmup", "cooldown"]


class _ExerciseBase(_Model):
    name: str = Field(
        min_length=1,
        max_length=120,
        description="Garmin catalog name, e.g. 'Barbell Bench Press'. Must "
        "match a catalog entry; use search_exercises to find it.",
    )
    exercise_id: ExerciseId | None = None
    kind: ExerciseKind = Field(
        default="exercise",
        description="warmup / cooldown mark the step as Garmin's Warm Up or Cool Down.",
    )
    reps: int | None = Field(default=None, ge=1, le=500)
    duration_seconds: int | None = Field(
        default=None,
        ge=1,
        le=3600,
        description="For timed sets (planks, carries), instead of reps.",
    )
    weight_kg: float | None = Field(default=None, gt=0, le=1000)
    weight_lb: float | None = Field(
        default=None, gt=0, le=2200, description="Instead of weight_kg."
    )
    rest_seconds: int | None = Field(
        default=None, ge=0, le=1800, description="Rest after the exercise."
    )
    notes: str | None = Field(default=None, max_length=512)

    @field_validator("rest_seconds")
    @classmethod
    def _no_zero_rest(cls, value: int | None) -> int | None:
        # Zero rest and no rest are the same workout; keep one spelling so a
        # read-back compares equal.
        return value or None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.reps is None) == (self.duration_seconds is None):
            raise ValueError("give exactly one of reps or duration_seconds")
        if self.weight_kg is not None and self.weight_lb is not None:
            raise ValueError("give weight_kg or weight_lb, not both")
        return self


class StrengthExercise(_ExerciseBase):
    """One exercise done for a number of sets, resting after each."""

    type: Literal["exercise"] = "exercise"
    sets: int = Field(ge=1, le=50)


class CircuitExercise(_ExerciseBase):
    pass


class Circuit(_Model):
    """Several exercises done in turn, the whole sequence repeated `rounds`
    times (what Garmin's editor makes of a repeat holding several exercises:
    supersets, circuits, a 5x5 done round by round)."""

    type: Literal["circuit"]
    rounds: int = Field(ge=1, le=50)
    exercises: list[CircuitExercise] = Field(min_length=2, max_length=10)


def _strength_item_type(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("type", "exercise"))
    return str(getattr(value, "type", "exercise"))


StrengthItem = Annotated[
    Annotated[StrengthExercise, Tag("exercise")] | Annotated[Circuit, Tag("circuit")],
    Discriminator(_strength_item_type),
]


class StrengthWorkout(_Model):
    name: str = Field(min_length=1, max_length=80)
    sport: Literal["strength"]
    description: str | None = Field(default=None, max_length=1024)
    exercises: list[StrengthItem] = Field(
        min_length=1,
        max_length=MAX_STRENGTH_EXERCISES,
        description="Exercises (type 'exercise', the default) and circuits "
        "(type 'circuit'), in order.",
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        total = len(self.all_exercises())
        if total > MAX_STRENGTH_EXERCISES:
            raise ValueError(
                f"{total} exercises counting those in circuits; the limit is "
                f"{MAX_STRENGTH_EXERCISES}"
            )
        return self

    def all_exercises(self) -> list[StrengthExercise | CircuitExercise]:
        out: list[StrengthExercise | CircuitExercise] = []
        for item in self.exercises:
            out += item.exercises if isinstance(item, Circuit) else [item]
        return out


class PaceTarget(_Model):
    type: Literal["pace"]
    fast_seconds: int = Field(gt=0, description="Faster end of the range.")
    slow_seconds: int = Field(gt=0, description="Slower end of the range.")
    per: Literal["km", "mi"] = "km"

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.fast_seconds > self.slow_seconds:
            raise ValueError("fast_seconds must be <= slow_seconds")
        scale = 1.0 if self.per == "km" else 1000 / METERS_PER_MILE
        for value in (self.fast_seconds, self.slow_seconds):
            per_km = value * scale
            if not MIN_PACE_SECONDS_PER_KM <= per_km <= MAX_PACE_SECONDS_PER_KM:
                raise ValueError(
                    f"pace {value} s/{self.per} is outside "
                    f"{MIN_PACE_SECONDS_PER_KM}–{MAX_PACE_SECONDS_PER_KM} s/km"
                )
        return self

    def seconds_per_km(self) -> tuple[float, float]:
        scale = 1.0 if self.per == "km" else 1000 / METERS_PER_MILE
        return self.fast_seconds * scale, self.slow_seconds * scale


class HeartRateTarget(_Model):
    type: Literal["heart_rate"]
    min_bpm: int = Field(ge=30, le=230)
    max_bpm: int = Field(ge=30, le=230)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.min_bpm > self.max_bpm:
            raise ValueError("min_bpm must be <= max_bpm")
        return self


class HeartRateZoneTarget(_Model):
    type: Literal["heart_rate_zone"]
    zone: int = Field(ge=1, le=5, description="One of the user's Garmin HR zones.")


class CadenceTarget(_Model):
    type: Literal["cadence"]
    min_spm: int = Field(ge=60, le=250)
    max_spm: int = Field(ge=60, le=250)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.min_spm > self.max_spm:
            raise ValueError("min_spm must be <= max_spm")
        return self


RunTarget = Annotated[
    PaceTarget | HeartRateTarget | HeartRateZoneTarget | CadenceTarget,
    Field(discriminator="type"),
]

RunStepType = Literal["warmup", "interval", "recovery", "cooldown", "rest"]


class RunStep(_Model):
    type: RunStepType
    duration_seconds: int | None = Field(default=None, ge=1, le=86_400)
    distance_meters: float | None = Field(default=None, gt=0, le=100_000)
    target: RunTarget | None = None
    notes: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.duration_seconds is not None and self.distance_meters is not None:
            raise ValueError(
                "give duration_seconds or distance_meters, not both "
                "(neither means the step ends on the lap button)"
            )
        if self.type == "rest" and self.target is not None:
            raise ValueError("a rest step can't have a target")
        return self


class RepeatBlock(_Model):
    type: Literal["repeat"]
    repeat_count: int = Field(ge=2, le=50)
    steps: list[RunStep] = Field(min_length=1, max_length=MAX_REPEAT_STEPS)

    @model_validator(mode="after")
    def _check(self) -> Self:
        for step in self.steps:
            if step.type in ("warmup", "cooldown"):
                raise ValueError(f"a {step.type} step can't be inside a repeat")
        return self


RunItem = Annotated[RunStep | RepeatBlock, Field(discriminator="type")]


class RunningWorkout(_Model):
    name: str = Field(min_length=1, max_length=80)
    sport: Literal["running"]
    description: str | None = Field(default=None, max_length=1024)
    steps: list[RunItem] = Field(min_length=1, max_length=MAX_RUN_STEPS)

    @model_validator(mode="after")
    def _check(self) -> Self:
        total = sum(
            len(item.steps) + 1 if isinstance(item, RepeatBlock) else 1
            for item in self.steps
        )
        if total > MAX_RUN_STEPS:
            raise ValueError(
                f"{total} steps (repeat blocks count themselves and their "
                f"steps); the limit is {MAX_RUN_STEPS}"
            )
        # Warmups lead and cooldowns trail; anything else is a mistake in the
        # order, not a workout.
        phase = 0
        for item in self.steps:
            step_phase = {"warmup": 0, "cooldown": 2}.get(item.type, 1)
            if step_phase < phase:
                if item.type == "warmup":
                    raise ValueError("warmup steps must come before all others")
                raise ValueError("cooldown steps must come after all others")
            phase = step_phase
        return self


Workout = Annotated[StrengthWorkout | RunningWorkout, Field(discriminator="sport")]


class ExerciseMatch(BaseModel):
    exercise_id: str
    name: str
    category: str


class WorkoutSummary(BaseModel):
    workout_id: int
    name: str
    sport: str = Field(description="Garmin's sport key, e.g. strength_training.")
    created: str | None = None
    updated: str | None = None
    estimated_duration_seconds: int | None = None


class WorkoutView(WorkoutSummary):
    description: str | None = None
    definition: StrengthWorkout | RunningWorkout | None = Field(
        default=None,
        description="The canonical form, or null if this server can't "
        "represent the workout (see unsupported_reason).",
    )
    unsupported_reason: str | None = None
