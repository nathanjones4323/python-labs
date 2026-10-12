"""`planning.timeline`: anchors, backward/forward placement, every violation.

Pure, so no database: each case is a list of stops, the legs between them,
and what the day should come out as.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from date_planner.db.models import Anchor
from date_planner.errors import IncompleteStop
from date_planner.planning.timeline import (
    Busy,
    StopInput,
    Timeline,
    ViolationType,
    ceil_to,
    floor_to,
    plan,
    round_to,
)

LA = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 24)
# USNO: sunset at Malibu on 2026-10-24 is 18:10 PDT.
SUNSET = datetime(2026, 10, 24, 18, 9, 33, tzinfo=LA)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute), LA)


def hhmm(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(LA).strftime("%H:%M")


def stop(
    title: str,
    anchor: Anchor = Anchor.NONE,
    duration: int | None = 60,
    *,
    start: datetime | None = None,
    trail: bool = False,
    offset: int = -30,
    sunset: datetime | None = SUNSET,
) -> StopInput:
    return StopInput(
        id=uuid.uuid5(uuid.NAMESPACE_URL, title),
        title=title,
        anchor=anchor,
        duration_min=duration,
        start_at=start,
        sunset_offset_min=offset,
        sunset=sunset,
        is_trail=trail,
    )


@dataclass
class Case:
    name: str
    stops: list[StopInput]
    legs: list[int | None]
    # (start, end) per stop, as HH:MM.
    expected: list[tuple[str | None, str | None]]
    violations: list[tuple[ViolationType, str]] = field(default_factory=list)
    max_leg: int = 25
    buffer: int = 10
    start_after: time | None = None
    end_before: time | None = None


def run(case: Case) -> Timeline:
    return plan(
        case.stops,
        case.legs,
        day=DAY,
        tz=LA,
        buffer_min=case.buffer,
        max_leg_minutes=case.max_leg,
        start_after=case.start_after,
        end_before=case.end_before,
    )


CASES = [
    Case(
        # Section 6 of the spec: hike, then the Getty at 13:00, then a sunset
        # dinner. 43 min Solstice -> Getty (haversine, padded), 18 min on to
        # dinner; "nearby" stretched to 45 for the coast.
        name="acceptance: hike before a fixed entry, dinner at sunset",
        stops=[
            stop("Solstice Canyon hike", duration=105, trail=True),
            stop("Getty Villa", Anchor.FIXED, 120, start=at(13)),
            stop("Sunset dinner", Anchor.SUNSET, 90),
        ],
        legs=[43, 18],
        max_leg=45,
        # Hike ends 13:00 - 43 - 10 = 12:07, floored to 12:05.
        # Dinner: 18:09:33 - 30 = 17:39:33, nearest 15 -> 17:45.
        expected=[("10:20", "12:05"), ("13:00", "15:00"), ("17:45", "19:15")],
    ),
    Case(
        name="no anchors: the day starts at 10:00 and walks forward",
        stops=[stop("Coffee", duration=30), stop("Beach", duration=90)],
        legs=[12],
        # 10:30 + 12 + 10 = 10:52 -> 10:55.
        expected=[("10:00", "10:30"), ("10:55", "12:25")],
    ),
    Case(
        name="no anchors: start_after sets the start",
        stops=[stop("Coffee", duration=30), stop("Beach", duration=90)],
        legs=[5],
        start_after=time(8, 30),
        expected=[("08:30", "09:00"), ("09:15", "10:45")],
    ),
    Case(
        name="flexible stops between two anchors walk forward",
        stops=[
            stop("Brunch", Anchor.FIXED, 60, start=at(10)),
            stop("Gallery", duration=45),
            stop("Dinner", Anchor.FIXED, 90, start=at(14)),
        ],
        legs=[15, 15],
        expected=[("10:00", "11:00"), ("11:25", "12:10"), ("14:00", "15:30")],
    ),
    Case(
        name="several stops before the first anchor walk backward",
        stops=[
            stop("Coffee", duration=20),
            stop("Hike", duration=90, trail=True),
            stop("Lunch", Anchor.FIXED, 60, start=at(12)),
        ],
        legs=[7, 20],
        # Hike ends 12:00 - 30 = 11:30, starts 10:00; coffee ends
        # 10:00 - 17 = 09:43 -> 09:40, starts 09:20.
        expected=[("09:20", "09:40"), ("10:00", "11:30"), ("12:00", "13:00")],
    ),
    Case(
        name="a sunset offset of zero still rounds to 15",
        stops=[stop("Viewpoint", Anchor.SUNSET, 30, offset=0)],
        legs=[],
        expected=[("18:15", "18:45")],
    ),
    Case(
        name="a sunset stop with no sunset known is placed as flexible",
        stops=[stop("Dinner", Anchor.SUNSET, 90, sunset=None)],
        legs=[],
        expected=[("10:00", "11:30")],
    ),
    Case(
        name="leg_exceeds_max",
        stops=[stop("Hike", duration=60), stop("Lunch", duration=60)],
        legs=[40],
        expected=[("10:00", "11:00"), ("11:50", "12:50")],
        violations=[(ViolationType.LEG_EXCEEDS_MAX, "Lunch")],
    ),
    Case(
        name="overlap: a flexible stop can't fit before the next anchor",
        stops=[
            stop("Museum", Anchor.FIXED, 120, start=at(10)),
            stop("Walk", duration=60),
            stop("Dinner", Anchor.FIXED, 60, start=at(13)),
        ],
        legs=[10, 10],
        # Walk 12:20-13:20, then 20 min more before dinner at 13:00.
        expected=[("10:00", "12:00"), ("12:20", "13:20"), ("13:00", "14:00")],
        violations=[(ViolationType.OVERLAP, "Walk")],
    ),
    Case(
        name="overlap: two anchors too close together",
        stops=[
            stop("Tour", Anchor.FIXED, 90, start=at(11)),
            stop("Lunch", Anchor.FIXED, 60, start=at(12, 30)),
        ],
        legs=[15],
        expected=[("11:00", "12:30"), ("12:30", "13:30")],
        violations=[(ViolationType.OVERLAP, "Tour")],
    ),
    Case(
        name="outside_window: before start_after and after end_before",
        stops=[
            stop("Hike", duration=120),
            stop("Lunch", Anchor.FIXED, 60, start=at(11)),
            stop("Show", Anchor.FIXED, 180, start=at(20)),
        ],
        legs=[10, 10],
        start_after=time(9, 0),
        end_before=time(22, 0),
        # Hike must end by 10:40, so starts 08:40.
        expected=[("08:40", "10:40"), ("11:00", "12:00"), ("20:00", "23:00")],
        violations=[
            (ViolationType.OUTSIDE_WINDOW, "Hike"),
            (ViolationType.OUTSIDE_WINDOW, "Show"),
        ],
    ),
    Case(
        name="daylight: a trail ending after sunset",
        stops=[
            stop("Lunch", Anchor.FIXED, 60, start=at(15)),
            stop("Ridge hike", duration=120, trail=True),
        ],
        legs=[15],
        expected=[("15:00", "16:00"), ("16:25", "18:25")],
        violations=[(ViolationType.DAYLIGHT, "Ridge hike")],
    ),
    Case(
        name="missing_duration: no end, and nothing walks forward off it",
        stops=[
            stop("Lunch", Anchor.FIXED, None, start=at(12)),
            stop("Walk", duration=30),
        ],
        legs=[5],
        expected=[("12:00", None), (None, None)],
        violations=[(ViolationType.MISSING_DURATION, "Lunch")],
    ),
    Case(
        name="missing_duration backward: no start, nothing earlier placed",
        stops=[
            stop("Coffee", duration=20),
            stop("Hike", duration=None),
            stop("Lunch", Anchor.FIXED, 60, start=at(12)),
        ],
        legs=[5, 15],
        expected=[(None, None), (None, "11:35"), ("12:00", "13:00")],
        violations=[(ViolationType.MISSING_DURATION, "Hike")],
    ),
    Case(
        name="an untimed leg counts as zero (the caller reports why)",
        stops=[stop("Coffee", duration=30), stop("Beach", duration=60)],
        legs=[None],
        expected=[("10:00", "10:30"), ("10:40", "11:40")],
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_timeline(case: Case) -> None:
    result = run(case)
    assert [(hhmm(s.start_at), hhmm(s.end_at)) for s in result.stops] == case.expected
    titles = {s.id: s.title for s in case.stops}
    assert sorted(
        (v.type, titles[v.stop_id]) for v in result.violations if v.stop_id
    ) == sorted(case.violations)


def test_the_acceptance_hike_is_in_daylight_and_reaches_the_getty() -> None:
    case = CASES[0]
    result = run(case)
    hike, getty, dinner = result.stops
    assert hike.end_at is not None and getty.start_at is not None
    # hike end + travel + buffer <= the Getty entry time.
    assert (getty.start_at - hike.end_at).total_seconds() / 60 >= 43 + 10
    assert not any(v.type is ViolationType.DAYLIGHT for v in result.violations)
    assert [s.travel_min_from_prev for s in result.stops] == [None, 43, 18]


def test_a_fixed_stop_needs_its_time() -> None:
    with pytest.raises(IncompleteStop, match="no start_at"):
        plan([stop("Getty", Anchor.FIXED, 120)], [], day=DAY, tz=LA)


def test_legs_must_match_the_stops() -> None:
    with pytest.raises(ValueError, match="one leg"):
        plan([stop("A"), stop("B")], [], day=DAY, tz=LA)


def test_order_is_never_changed() -> None:
    stops = [stop(name) for name in ("C", "A", "B")]
    result = plan(stops, [5, 5], day=DAY, tz=LA)
    assert [s.id for s in result.stops] == [s.id for s in stops]


@pytest.mark.parametrize(
    ("moment", "rounded", "floored", "ceiled"),
    [
        (at(17, 39), "17:45", "17:35", "17:40"),
        (at(17, 37), "17:30", "17:35", "17:40"),
        (at(17, 40), "17:45", "17:40", "17:40"),
    ],
)
def test_rounding(moment: datetime, rounded: str, floored: str, ceiled: str) -> None:
    assert hhmm(round_to(moment, 15)) == rounded
    assert hhmm(floor_to(moment, 5)) == floored
    assert hhmm(ceil_to(moment, 5)) == ceiled


def test_a_stop_on_a_busy_block_is_a_calendar_conflict() -> None:
    brunch = Busy("Brunch with Sam", at(11), at(12, 30))
    stops = [
        stop("Getty", Anchor.FIXED, 120, start=at(12)),
        stop("Beach", duration=60),
    ]
    result = plan(stops, [15], day=DAY, tz=LA, busy=[brunch])
    conflicts = [
        v for v in result.violations if v.type is ViolationType.CALENDAR_CONFLICT
    ]
    assert [v.stop_id for v in conflicts] == [stops[0].id]
    assert "given for it" in conflicts[0].message
    assert "11:00-12:30" in conflicts[0].message
    # Checked, never moved.
    assert hhmm(result.stops[0].start_at) == "12:00"


def test_touching_a_busy_block_is_not_a_calendar_conflict() -> None:
    stops = [stop("Getty", Anchor.FIXED, 120, start=at(13))]
    busy = [Busy("Brunch", at(11), at(13)), Busy("Call", at(15), at(16))]
    result = plan(stops, [], day=DAY, tz=LA, busy=busy)
    assert result.violations == []
