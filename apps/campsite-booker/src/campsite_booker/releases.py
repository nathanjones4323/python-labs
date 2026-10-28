"""When a campground's next dates go on sale, and which watches are waiting
for them.

Each campground opens its booking window on a schedule:
`/api/camps/campgrounds/{id}/releases` gives the arrival dates already
open (through `current_release.end`) and the next release: when it happens
and the last arrival date it opens. Scorpion opens one more day every
morning at 10:00 Eastern; Upper Pines a month at a time on the 15th. Popular
sites go within seconds of a release, so a watch waiting on one is
"armed": the watcher checks the session is signed in beforehand, then polls
every couple of seconds from just before the release until the dates show
up (see `bot.CampsiteBot._snipe`).

Pure functions over the parsed response; the watcher does the timing.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from campsite_booker.availability import StayCriteria
from campsite_booker.recgov import NYR, OPEN, Campsite


@dataclass(frozen=True)
class Release:
    """The booking window now, and the next time it grows."""

    # The last arrival date already on sale.
    open_through: date
    # When the next dates go on sale, and the last arrival date they reach.
    # None when none is scheduled (a closed or seasonal campground).
    next_at: datetime | None
    next_open_through: date | None

    def opens(self, day: date) -> bool:
        """Whether the next release is what puts `day` on sale."""
        return (
            self.next_at is not None
            and self.next_open_through is not None
            and self.open_through < day <= self.next_open_through
        )


def _date(value: str | None) -> date | None:
    # "2027-03-14T00:00:00Z": a campground-local date, like availability's.
    return date.fromisoformat(value[:10]) if value else None


def parse_releases(payload: dict[str, Any], now: datetime) -> Release:
    current = payload.get("current_release") or {}
    following = payload.get("next_release") or {}
    open_through = _date(current.get("end")) or date.min
    next_at = (
        datetime.fromisoformat(following["release_time"])
        if following.get("release_time")
        else None
    )
    next_through = _date(following.get("end"))
    # A closed campground reports a "next" release a year out that opens
    # nothing new; treat that, and one already past, as none.
    if (
        next_at is None
        or next_at <= now
        or next_through is None
        or next_through <= open_through
    ):
        return Release(open_through, None, None)
    return Release(open_through, next_at, next_through)


def waiting_on(criteria: StayCriteria, release: Release, today: date) -> list[date]:
    """The watch's arrival dates the next release puts on sale."""
    return [day for day in criteria.arrivals(today) if release.opens(day)]


def released(sites: dict[str, Campsite], release: Release) -> bool:
    """Whether the release has happened: the first day it opens is no longer
    `Open` or `NYR` at any site. (Other statuses don't tell: a seasonal site
    is `Closed` either way, and a stay arriving inside the old window already
    shows `Reserved` on it.)"""
    day = date.fromordinal(release.open_through.toordinal() + 1)
    statuses = {s.nights.get(day) for s in sites.values()} - {None}
    return bool(statuses) and not statuses & {OPEN, NYR}
