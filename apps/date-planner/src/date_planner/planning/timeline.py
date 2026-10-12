"""Place a day's stops in time, in the order they're given, and say what's wrong.

This checks and places; it never reorders. The agent picks the order
(`position`); this works out the times that order implies:

1. Anchors first. A `fixed` stop keeps its `start_at`; a `sunset` stop
   starts at sunset + `sunset_offset_min`, rounded to the nearest 15 minutes.
2. Flexible stops fill in around them, each leg costing its travel time plus
   `buffer_min`: walking *backward* from the first anchor, *forward* from
   each anchor after that. With no anchors at all, the day starts at
   `start_after` (or 10:00).
3. Every stop ends `est_duration_min` after it starts.

Flexible times are rounded to 5 minutes — away from the neighbouring anchor,
so the rounding never eats into a leg. Problems come back as violations,
never as silent fixes: it's the agent's job to change the order or a
duration and run it again.

Busy blocks — events already on the shared calendar — are only checked
against: a stop that lands on one is a `calendar_conflict`, never moved.

Missing data doesn't stop the rest of the day being worked out. A leg that
couldn't be timed counts as zero minutes (the caller reports
`missing_coordinates`), and a stop with no duration has no end, so a
flexible stop placed off it stays unplaced.
"""

import enum
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from date_planner.db.models import Anchor
from date_planner.errors import IncompleteStop

DEFAULT_DAY_START = time(10, 0)


class ViolationType(enum.StrEnum):
    LEG_EXCEEDS_MAX = "leg_exceeds_max"
    OVERLAP = "overlap"
    OUTSIDE_WINDOW = "outside_window"
    MISSING_DURATION = "missing_duration"
    MISSING_COORDINATES = "missing_coordinates"
    DAYLIGHT = "daylight"
    CALENDAR_CONFLICT = "calendar_conflict"


@dataclass(frozen=True)
class Violation:
    type: ViolationType
    message: str
    stop_id: uuid.UUID | None = None


@dataclass(frozen=True)
class StopInput:
    id: uuid.UUID
    title: str
    anchor: Anchor
    duration_min: int | None
    # Only read for `fixed` stops.
    start_at: datetime | None = None
    sunset_offset_min: int = -30
    # Sunset at this stop's venue; None if it couldn't be worked out, in
    # which case a `sunset` stop is placed as if it were flexible.
    sunset: datetime | None = None
    # Trails get the daylight check.
    is_trail: bool = False


@dataclass(frozen=True)
class Busy:
    """An event already on the calendar that the day has to fit around."""

    title: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class PlacedStop:
    id: uuid.UUID
    start_at: datetime | None
    end_at: datetime | None
    travel_min_from_prev: int | None


@dataclass
class Timeline:
    stops: list[PlacedStop]
    violations: list[Violation] = field(default_factory=list)


def _minutes(moment: datetime) -> float:
    return (
        moment.hour * 60 + moment.minute + moment.second / 60 + moment.microsecond / 6e7
    )


def _at_minute(moment: datetime, minutes: float) -> datetime:
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(minutes=minutes)


def round_to(moment: datetime, step: int) -> datetime:
    return _at_minute(moment, round(_minutes(moment) / step) * step)


def floor_to(moment: datetime, step: int) -> datetime:
    return _at_minute(moment, (_minutes(moment) // step) * step)


def ceil_to(moment: datetime, step: int) -> datetime:
    return _at_minute(moment, -(-_minutes(moment) // step) * step)


def _end(start: datetime | None, duration: int | None) -> datetime | None:
    if start is None or duration is None:
        return None
    return start + timedelta(minutes=duration)


def _clock(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def plan(
    stops: list[StopInput],
    legs: list[int | None],
    *,
    day: date,
    tz: ZoneInfo,
    buffer_min: int = 10,
    max_leg_minutes: int = 25,
    start_after: time | None = None,
    end_before: time | None = None,
    busy: list[Busy] | None = None,
) -> Timeline:
    """Times for `stops`, in order; `legs[i]` is the drive from stop i to i+1.

    Raises `IncompleteStop` for a `fixed` stop with no `start_at`: there's
    nothing to anchor the day on, and guessing would defeat the point.
    """
    if len(legs) != max(len(stops) - 1, 0):
        raise ValueError("Need exactly one leg between each pair of stops")
    n = len(stops)
    starts: list[datetime | None] = [None] * n
    ends: list[datetime | None] = [None] * n
    violations: list[Violation] = []

    def gap(i: int) -> int:
        """Travel plus buffer into stop `i` from the stop before it."""
        return (legs[i - 1] or 0) + buffer_min

    anchors: list[int] = []
    for i, stop in enumerate(stops):
        if stop.anchor is Anchor.FIXED:
            if stop.start_at is None:
                raise IncompleteStop(
                    f'"{stop.title}" is fixed but has no start_at; ask for the '
                    "booked time and set it with update_stop."
                )
            starts[i] = stop.start_at.astimezone(tz)
            anchors.append(i)
        elif stop.anchor is Anchor.SUNSET and stop.sunset is not None:
            starts[i] = round_to(
                stop.sunset.astimezone(tz) + timedelta(minutes=stop.sunset_offset_min),
                15,
            )
            anchors.append(i)
        ends[i] = _end(starts[i], stop.duration_min)

    if not anchors and n:
        starts[0] = datetime.combine(day, start_after or DEFAULT_DAY_START, tz)
        ends[0] = _end(starts[0], stops[0].duration_min)
        first = 0
    else:
        first = anchors[0] if anchors else 0

    # Backward from the first anchor.
    for i in range(first - 1, -1, -1):
        following = starts[i + 1]
        if following is None:
            break
        latest_end = floor_to(following - timedelta(minutes=gap(i + 1)), 5)
        ends[i] = latest_end
        duration = stops[i].duration_min
        if duration is None:
            # Without a duration there's no start, so nothing earlier can be
            # walked back from it.
            break
        starts[i] = latest_end - timedelta(minutes=duration)

    # Forward from the first anchor (or the day's start).
    for i in range(first + 1, n):
        previous_end = ends[i - 1]
        earliest = (
            None if previous_end is None else previous_end + timedelta(minutes=gap(i))
        )
        if i in anchors:
            anchored = starts[i]
            if (
                previous_end is not None
                and earliest is not None
                and anchored is not None
                and earliest > anchored
            ):
                late = int((earliest - anchored).total_seconds() // 60)
                violations.append(
                    Violation(
                        ViolationType.OVERLAP,
                        f'"{stops[i - 1].title}" ends at {_clock(previous_end)}'
                        f" and the leg plus buffer is {gap(i)} min, so it runs "
                        f'{late} min into "{stops[i].title}" at {_clock(anchored)}.',
                        stops[i - 1].id,
                    )
                )
            continue
        if earliest is None:
            continue
        starts[i] = ceil_to(earliest, 5)
        ends[i] = _end(starts[i], stops[i].duration_min)

    for i, stop in enumerate(stops):
        if stop.duration_min is None:
            violations.append(
                Violation(
                    ViolationType.MISSING_DURATION,
                    f'"{stop.title}" has no est_duration_min, so it has no end '
                    "time; set one with update_stop.",
                    stop.id,
                )
            )
    for i, minutes in enumerate(legs):
        if minutes is not None and minutes > max_leg_minutes:
            violations.append(
                Violation(
                    ViolationType.LEG_EXCEEDS_MAX,
                    f'{minutes} min from "{stops[i].title}" to '
                    f'"{stops[i + 1].title}" is over the {max_leg_minutes}-min '
                    "limit for this date.",
                    stops[i + 1].id,
                )
            )
    window_start = (
        datetime.combine(day, start_after, tz) if start_after is not None else None
    )
    window_end = (
        datetime.combine(day, end_before, tz) if end_before is not None else None
    )
    for i, stop in enumerate(stops):
        start, end = starts[i], ends[i]
        if window_start is not None and start is not None and start < window_start:
            violations.append(
                Violation(
                    ViolationType.OUTSIDE_WINDOW,
                    f'"{stop.title}" starts at {_clock(start)}, before the '
                    f"day's start of {_clock(window_start)}.",
                    stop.id,
                )
            )
        if window_end is not None and end is not None and end > window_end:
            violations.append(
                Violation(
                    ViolationType.OUTSIDE_WINDOW,
                    f'"{stop.title}" ends at {_clock(end)}, after the '
                    f"day's end of {_clock(window_end)}.",
                    stop.id,
                )
            )
        if (
            stop.is_trail
            and end is not None
            and stop.sunset is not None
            and end > stop.sunset
        ):
            violations.append(
                Violation(
                    ViolationType.DAYLIGHT,
                    f'"{stop.title}" is a trail ending at {_clock(end)}, after '
                    f"sunset at {_clock(stop.sunset.astimezone(tz))}.",
                    stop.id,
                )
            )

    for i, stop in enumerate(stops):
        start, end = starts[i], ends[i]
        if start is None:
            continue
        # A stop with no end still occupies its start.
        until = end if end is not None else start + timedelta(minutes=1)
        for block in busy or []:
            if start < block.end and block.start < until:
                chosen = (
                    "the time given for it"
                    if stop.anchor is Anchor.FIXED
                    else "the time worked out for it"
                )
                violations.append(
                    Violation(
                        ViolationType.CALENDAR_CONFLICT,
                        f'"{stop.title}" at {_clock(start)}'
                        + (f"-{_clock(end)}" if end is not None else "")
                        + f' ({chosen}) overlaps "{block.title}", '
                        f"{_clock(block.start.astimezone(tz))}-"
                        f"{_clock(block.end.astimezone(tz))}.",
                        stop.id,
                    )
                )

    return Timeline(
        stops=[
            PlacedStop(
                id=stop.id,
                start_at=starts[i],
                end_at=ends[i],
                travel_min_from_prev=None if i == 0 else legs[i - 1],
            )
            for i, stop in enumerate(stops)
        ],
        violations=violations,
    )
