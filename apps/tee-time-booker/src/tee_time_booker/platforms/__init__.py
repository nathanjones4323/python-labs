"""Adapters, one per booking site.

Adding a site: write `platforms/<site>.py` with a `BookingPlatform` subclass,
add it to `_ADAPTERS` below, and give its courses `platform="<site>"` in
`courses.py`. Nothing else in the app knows which site a booking runs against.
"""

from tee_time_booker.browser import BrowserSession
from tee_time_booker.config import Credentials
from tee_time_booker.courses import CourseConfig
from tee_time_booker.platforms.base import BookingPlatform
from tee_time_booker.platforms.foreup import ForeUpPlatform
from tee_time_booker.platforms.golfnow import GolfNowPlatform

_ADAPTERS: tuple[type[BookingPlatform], ...] = (ForeUpPlatform, GolfNowPlatform)

PLATFORMS: dict[str, type[BookingPlatform]] = {p.name: p for p in _ADAPTERS}


def get_platform(name: str) -> type[BookingPlatform]:
    try:
        return PLATFORMS[name]
    except KeyError:
        known = ", ".join(sorted(PLATFORMS))
        raise ValueError(
            f"Unknown platform {name!r}. Known platforms: {known}"
        ) from None


def build_platform(
    course: CourseConfig, credentials: Credentials, session: BrowserSession
) -> BookingPlatform:
    """The adapter that books `course`, ready to drive `session`."""
    return get_platform(course.platform)(course, credentials, session)


__all__ = [
    "PLATFORMS",
    "BookingPlatform",
    "ForeUpPlatform",
    "GolfNowPlatform",
    "build_platform",
    "get_platform",
]
