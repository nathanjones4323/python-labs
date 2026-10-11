"""How a venue takes a reservation, and the window it opens one in."""

import enum


class BookingChannel(enum.StrEnum):
    RESY = "resy"
    OPENTABLE = "opentable"
    TOCK = "tock"
    SEVENROOMS = "sevenrooms"
    PHONE = "phone"
    WALK_IN = "walk_in"
    # Timed entry booked on the venue's own site (e.g. the Getty Villa).
    # `platform_slug` holds the URL of that booking page.
    TICKETED = "ticketed"
    UNKNOWN = "unknown"


# The channels a prefilled link can be built for (`booking.links`).
ONLINE_CHANNELS = frozenset(
    {
        BookingChannel.RESY,
        BookingChannel.OPENTABLE,
        BookingChannel.TOCK,
        BookingChannel.SEVENROOMS,
    }
)


class WindowUnit(enum.StrEnum):
    """What a booking window counts.

    Months matter: Nobu Malibu opens "one month to the exact calendar date",
    which is not 30 days — see `booking.window`.
    """

    DAYS = "days"
    MONTHS = "months"


class WindowState(enum.StrEnum):
    OPEN = "open"
    NOT_YET_OPEN = "not_yet_open"
    UNKNOWN = "unknown"  # the restaurant's window isn't recorded
