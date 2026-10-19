"""Result shapes shared by the tools: writes, account, activities, metrics,
devices."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

WriteStatus = Literal["dry_run", "approval_required", "verified", "failed", "unknown"]


class ChangePreview(BaseModel):
    summary: str = Field(description="One line saying what the write will do.")
    details: list[str] = Field(
        default_factory=list, description="The change, line by line."
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Things the user should know before approving (e.g. "
        "another workout already on that day).",
    )


class Verification(BaseModel):
    status: Literal["verified", "mismatch", "not_found", "unreadable"]
    checked: str = Field(description="What was read back from Garmin.")
    mismatches: list[str] = Field(default_factory=list)


class WriteResult(BaseModel):
    """The outcome of a write tool call.

    `status` is the thing to branch on:

    - `dry_run`: nothing was sent; `preview` shows what would happen.
    - `approval_required`: nothing was sent. Show `preview` to the user and,
      only if they approve, call the same tool with the same arguments plus
      `confirmation_token`.
    - `verified`: done, and the read-back matches.
    - `failed`: Garmin definitely didn't apply it; `error` says why.
    - `unknown`: it may have been applied. Do NOT retry. Check with the read
      tools (or ask the user to look in Garmin Connect) first.
    """

    operation: str
    status: WriteStatus
    message: str = Field(description="What happened and what to do next.")
    preview: ChangePreview
    confirmation_token: str | None = None
    token_expires_at: datetime | None = None
    result: dict[str, Any] | None = Field(
        default=None, description="Ids of what was created or changed."
    )
    verification: Verification | None = None
    error: str | None = None


class AccountStatus(BaseModel):
    authenticated: bool
    connected: bool
    display_name: str | None = None
    unit_system: str | None = None
    token_file: str
    token_file_present: bool
    dry_run: bool = Field(
        description="True while GARMIN_DRY_RUN is on: every write only previews."
    )
    error: str | None = None
    action: str | None = Field(
        default=None, description="What to do to fix `error`, if anything."
    )


class ActivitySummary(BaseModel):
    activity_id: int
    name: str | None = None
    activity_type: str | None = None
    start_time_local: str | None = None
    duration_seconds: float | None = None
    distance_meters: float | None = None
    calories: float | None = None
    average_hr: float | None = None
    max_hr: float | None = None
    average_speed_mps: float | None = None
    elevation_gain_meters: float | None = None


class ExerciseSet(BaseModel):
    exercise: str | None = Field(description="CATEGORY/EXERCISE as Garmin logged it.")
    reps: int | None = None
    weight_kg: float | None = None
    duration_seconds: float | None = None


class Lap(BaseModel):
    index: int
    duration_seconds: float | None = None
    distance_meters: float | None = None
    average_hr: float | None = None
    average_speed_mps: float | None = None


class ActivityDetails(ActivitySummary):
    description: str | None = None
    moving_duration_seconds: float | None = None
    average_cadence: float | None = None
    training_effect_aerobic: float | None = None
    training_effect_anaerobic: float | None = None
    laps: list[Lap] = Field(default_factory=list)
    exercise_sets: list[ExerciseSet] = Field(
        default_factory=list, description="Strength activities only; rest sets omitted."
    )


Metric = Literal[
    "daily_summary",
    "sleep",
    "hrv",
    "training_readiness",
    "training_status",
    "vo2max",
]


class DailyMetrics(BaseModel):
    date: date
    values: dict[Metric, dict[str, Any] | None] = Field(
        description="One entry per requested metric; null when Garmin has "
        "nothing for that day (no device worn, metric unsupported)."
    )


class TrainingMetrics(BaseModel):
    metrics: list[Metric]
    units: dict[str, str] = Field(
        description="The unit of each field, by suffix (e.g. _seconds)."
    )
    days: list[DailyMetrics]


class DeviceSummary(BaseModel):
    device_id: int
    name: str | None = None
    model: str | None = None
    last_used: bool = False
    workout_sports: dict[str, bool] = Field(
        default_factory=dict,
        description="Which workout sports the device says it can run "
        "(strength, running, ...). A sport missing here is unknown.",
    )
    max_workouts: int | None = Field(
        default=None, description="How many workouts the device can hold."
    )


class DevicePushResult(BaseModel):
    upload_status: Literal["accepted", "failed", "unknown", "not_attempted"]
    sync_status: Literal["pending_on_garmin", "not_in_queue", "unknown"] = Field(
        description="pending_on_garmin: Garmin queued it and the watch "
        "hasn't fetched it yet. not_in_queue: either the watch already took "
        "it or Garmin dropped it; Garmin doesn't say which. Delivery to the "
        "watch itself can only be confirmed on the watch."
    )
    device_id: int
    workout_id: int
