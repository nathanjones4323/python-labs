"""The courses this app can book, and the preferences it books them with.

One entry per course. The `platform` field names the adapter that drives its
booking site (see `tee_time_booker.platforms`); the rest is what that adapter
needs to find the course plus the preferences the run applies.

Adding a course is adding an entry here. Adding a *site* is adding an adapter
and, if the site needs identifiers these classes don't carry, a subclass.
"""

from dataclasses import dataclass
from datetime import date, time
from zoneinfo import ZoneInfo

from tee_time_booker.models import SearchCriteria


@dataclass(frozen=True, kw_only=True)
class Preferences:
    """Which day, which time window, how big a group."""

    # Book the Nth upcoming occurrence of `target_weekday` (Monday=0 ...
    # Sunday=6); "upcoming" includes today if today is that weekday.
    target_weekday: int
    weeks_ahead: int
    # Acceptable tee-off window (course-local), inclusive on both ends.
    earliest: time
    latest: time
    holes: int
    players: int
    # Within the window, take the earliest slot. False takes the latest.
    prefer_earlier: bool = True


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
    """A bookable course: where it is, who runs its booking, how we like to play."""

    key: str  # CLI / env name, e.g. "soule-park"
    name: str
    platform: str  # adapter name, e.g. "foreup"
    timezone: ZoneInfo
    preferences: Preferences
    release: ReleaseSchedule | None
    # Environment variables holding this course's login. Separate courses on
    # the same platform can use separate accounts.
    email_env: str
    password_env: str

    def criteria(self, target_date: date) -> SearchCriteria:
        """The search these preferences describe for `target_date`."""
        prefs = self.preferences
        return SearchCriteria(
            target_date=target_date,
            earliest=prefs.earliest,
            latest=prefs.latest,
            players=prefs.players,
            holes=prefs.holes,
            prefer_earlier=prefs.prefer_earlier,
        )


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
    preferences=Preferences(
        target_weekday=5,  # Saturday
        weeks_ahead=3,  # the 3rd upcoming Saturday
        earliest=time(8, 0),
        latest=time(14, 0),
        holes=18,
        players=4,
    ),
    # foreUP opens each day's tee sheet at course-local midnight, 20 days
    # ahead (its "21 day" window counts today).
    release=ReleaseSchedule(opens_at=time(0, 0), pre_release_start=time(20, 0)),
    email_env="SOULE_EMAIL",
    password_env="SOULE_PASSWORD",
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
    preferences=Preferences(
        target_weekday=5,  # Saturday
        weeks_ahead=1,  # GolfNow lists roughly two weeks out
        earliest=time(7, 0),
        latest=time(11, 0),
        holes=9,  # Saticoy sells 9-hole rates on GolfNow
        players=4,
    ),
    # GolfNow releases on a rolling window rather than at a fixed local time.
    release=None,
    email_env="GOLFNOW_EMAIL",
    password_env="GOLFNOW_PASSWORD",
    facility_id=1226,
    seo_name="1226-saticoy-regional-golf-course",
    latitude=34.283855,
    longitude=-119.154724,
)

COURSES: dict[str, CourseConfig] = {c.key: c for c in (SOULE_PARK, SATICOY)}

DEFAULT_COURSE = SOULE_PARK.key


def get_course(key: str) -> CourseConfig:
    try:
        return COURSES[key]
    except KeyError:
        known = ", ".join(sorted(COURSES))
        raise ValueError(f"Unknown course {key!r}. Known courses: {known}") from None
