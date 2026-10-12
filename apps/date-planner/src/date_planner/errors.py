"""The failures a caller can do something about.

Tools turn these into structured MCP errors (`tools.as_tool_error`), keeping
the class name as the error code so an agent can branch on it instead of
reading prose.
"""


class DatePlannerError(Exception):
    """Base class for everything a tool reports back to its caller."""


class NotFound(DatePlannerError):
    """A lookup by id matched nothing."""


class InvalidTransition(DatePlannerError):
    """A date was asked for a status that isn't set by hand, or to leave
    `completed` / `cancelled`."""


class DetectionFailed(DatePlannerError):
    """A venue's booking channel couldn't be worked out — no website, or
    nothing fetchable."""


class UnsupportedChannel(DatePlannerError):
    """There is no deep link to build for this channel (phone, walk-in,
    unknown)."""


class MissingPlatformSlug(DatePlannerError):
    """The channel supports deep links but the venue has no slug for it."""


class MissingCoordinates(DatePlannerError):
    """A venue has no lat/lng, so nothing can be timed to or from it. Look
    the coordinates up and save them with `upsert_venue`."""


class IncompleteStop(DatePlannerError):
    """A stop lacks what the call needs — a venue, a start time — or a fixed
    stop has no time to anchor the day on."""


class TravelFailed(DatePlannerError):
    """The routing provider couldn't time a leg: unreachable, no route, or
    not configured."""


class PlacesFailed(DatePlannerError):
    """Google Places couldn't be asked: no `GOOGLE_PLACES_API_KEY`, a bad
    key, the API not enabled, or no answer."""


class AmbiguousVenue(DatePlannerError):
    """A venue name fits several stored venues (branches of a chain). Pass
    `lat`/`lng` or the venue's id to say which."""


class DuplicateVenue(DatePlannerError):
    """Google says this venue is a place we already have under another row.
    Use that venue instead of recording a second one."""


class MissingGooglePlace(DatePlannerError):
    """The venue has no Google Place ID on file. Run `find_google_place`."""


class OutsideWidgetRange(DatePlannerError):
    """The Resy widget only shows today to 60 days out, for 1–15 people.
    Hand over `build_booking_link` instead."""
