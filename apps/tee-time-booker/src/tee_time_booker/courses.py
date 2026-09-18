"""The shape of a bookable course, and the seed data for the ones we know.

Courses live in the `courses` table; `tee_time_booker.db` turns a row into one
of these dataclasses (the subclass an adapter needs, picked by `platform`) and
back. The constants at the bottom are what `tee_time_booker.seed` loads into
an empty database, and what the tests use as fixtures.

A course says where it is and how its site releases days. *How* a user wants
to play it (weekday, window, group size) is a `Preferences` on their booking
plan, not a property of the course.

Adding a course is adding a seed entry. Adding a *site* is adding an adapter
and, if the site needs identifiers these classes don't carry, a subclass.
"""

from dataclasses import dataclass, fields
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True, kw_only=True)
class ReleaseSchedule:
    """When a course opens a new day's tee sheet.

    Courses that release at a fixed local time (foreUP's midnight drop) have
    one; sites with rolling availability (GolfNow) leave it unset and book
    whatever is already open.
    """

    # Course-local time of day the sheet opens.
    opens_at: time
    # A run starting at or after this time is waiting for the *coming*
    # release, so it belongs to the day about to begin, not the one ending.
    pre_release_start: time


@dataclass(frozen=True, kw_only=True)
class CourseConfig:
    """A bookable course: where it is and who runs its booking."""

    key: str  # stable identifier, e.g. "soule-park"
    name: str
    platform: str  # adapter name, e.g. "foreup"
    timezone: ZoneInfo
    release: ReleaseSchedule | None
    # How far ahead the site takes bookings, when that's fixed and known: the
    # last bookable day is this many days after the day a run books from
    # (`dates.release_day`). None means the adapter asks the site instead
    # (`BookingPlatform.last_bookable_day`).
    booking_window_days: int | None

    def platform_config(self) -> dict[str, Any]:
        """The adapter-specific identifiers: every field a subclass adds.

        This is what the `courses.platform_config` JSONB column holds, so a
        new site's identifiers need no schema change.
        """
        base = {f.name for f in fields(CourseConfig)}
        return {
            f.name: getattr(self, f.name) for f in fields(self) if f.name not in base
        }


@dataclass(frozen=True, kw_only=True)
class ForeUpCourse(CourseConfig):
    """A course booked through foreUP's public booking page."""

    # Identifiers from the course's foreUP booking URL and its tee sheet.
    course_id: str
    booking_class: str
    # The label on the booking-class button matching `booking_class`.
    booking_class_name: str
    schedule_id: str


@dataclass(frozen=True, kw_only=True)
class GolfNowCourse(CourseConfig):
    """A course booked through GolfNow."""

    facility_id: int
    # The slug in GolfNow's facility URLs, e.g. "1226-saticoy-regional-golf-course".
    seo_name: str
    # GolfNow's search API wants the facility's coordinates alongside its id.
    latitude: float
    longitude: float


PACIFIC = ZoneInfo("America/Los_Angeles")

SOULE_PARK = ForeUpCourse(
    key="soule-park",
    name="Soule Park",
    platform="foreup",
    timezone=PACIFIC,
    # foreUP opens each day's tee sheet at course-local midnight, 20 days
    # ahead (its "21 day" window counts today).
    release=ReleaseSchedule(opens_at=time(0, 0), pre_release_start=time(20, 0)),
    booking_window_days=20,
    course_id="19620",
    booking_class="4718",
    booking_class_name="Regular",
    schedule_id="1945",
)

SATICOY = GolfNowCourse(
    key="saticoy",
    name="Saticoy Regional Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    # GolfNow releases on a rolling window rather than at a fixed local time.
    release=None,
    # Each GolfNow facility sets its own window (Saticoy lists a week ahead,
    # Sterling Hills three or more), and the site nowhere states it, so the
    # adapter measures it at run time.
    booking_window_days=None,
    facility_id=1226,
    seo_name="1226-saticoy-regional-golf-course",
    latitude=34.283855,
    longitude=-119.154724,
)

# Loaded into the `courses` table by `python -m tee_time_booker.seed`.
SEED_COURSES: tuple[CourseConfig, ...] = (SOULE_PARK, SATICOY)
