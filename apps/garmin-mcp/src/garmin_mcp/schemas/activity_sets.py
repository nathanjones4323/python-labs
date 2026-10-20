"""The sets logged on a recorded strength activity, in one form for reading
(`get_activity_details`) and writing (`update_activity_exercise_sets`).

A set is an exercise set or a rest, in the order they happened. Garmin
stores each with a start time; the canonical form keeps only durations, and
a write lays the sets end to end from the activity's first set. So the
recorded timeline survives an edit as long as each set keeps its duration.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from garmin_mcp.schemas.workouts import ExerciseId

# Garmin's own logs for an hour-long session run to 40-odd sets, rests
# included.
MAX_ACTIVITY_SETS = 150


class ActivitySet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["exercise", "rest"] = "exercise"
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        description="Exercise sets only: a Garmin catalog name, as for "
        "workouts (or give exercise_id). Null on a read when Garmin logged "
        "an exercise that isn't in the catalog; exercise_id says what it was.",
    )
    exercise_id: ExerciseId | None = None
    reps: int | None = Field(
        default=None,
        ge=0,
        le=500,
        description="Exercise sets only. Null for a timed set or reps the "
        "watch didn't count.",
    )
    weight_kg: float | None = Field(default=None, ge=0, le=1000)
    weight_lb: float | None = Field(
        default=None, ge=0, le=2200, description="Instead of weight_kg."
    )
    duration_seconds: float = Field(ge=0, le=7200)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.weight_kg is not None and self.weight_lb is not None:
            raise ValueError("give weight_kg or weight_lb, not both")
        if self.type == "rest":
            if any(
                v is not None
                for v in (
                    self.name,
                    self.exercise_id,
                    self.reps,
                    self.weight_kg,
                    self.weight_lb,
                )
            ):
                raise ValueError("a rest has only duration_seconds")
        return self
