"""Calendar entries as the tools return them."""

from datetime import date

from pydantic import BaseModel, Field


class ScheduledWorkout(BaseModel):
    scheduled_workout_id: int = Field(
        description="The calendar entry's id; pass it to reschedule_workout."
    )
    workout_id: int | None
    date: date
    title: str | None = None
    sport: str | None = None


class CalendarActivity(BaseModel):
    """A recorded activity on the calendar: what actually got done that day."""

    activity_id: int
    date: date
    title: str | None = None
    activity_type: str | None = None


class Calendar(BaseModel):
    start_date: date
    end_date: date
    scheduled_workouts: list[ScheduledWorkout]
    activities: list[CalendarActivity] = Field(
        description="Recorded activities in the range, for judging whether a "
        "scheduled workout was done. Garmin doesn't link the two here."
    )
