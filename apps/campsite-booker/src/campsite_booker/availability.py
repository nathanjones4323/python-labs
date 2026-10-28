"""Which stays a campground's availability offers for what you're after.

Pure functions over `recgov.Campsite`s: the search, the watcher and the
tests all go through `find_stays`.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from campsite_booker.recgov import AVAILABLE, OPEN, Campsite, Stay

# Longer than any campground's stay limit; a guard against a typo, not a rule.
MAX_NIGHTS = 21
MAX_ARRIVAL_SPAN_DAYS = 366


@dataclass(frozen=True)
class StayCriteria:
    """What a stay must look like.

    Exact dates are `arrival_from == arrival_to`. A flexible trip is a range
    of arrival dates, optionally only some weekdays (0 = Monday ... 6 =
    Sunday), each staying `nights` nights.
    """

    arrival_from: date
    arrival_to: date
    nights: int
    arrival_weekdays: frozenset[int] = frozenset()
    # Sites that hold fewer people than this are skipped.
    group_size: int | None = None
    # Only these sites; empty means any site at the campground.
    campsite_ids: frozenset[str] = frozenset()
    # Words the site type must contain one of, e.g. ("TENT",) or ("RV",);
    # case-insensitive. Empty means any type.
    site_types: tuple[str, ...] = ()
    # Group sites are for 10+ people and cost accordingly: off unless asked.
    include_group_sites: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.nights <= MAX_NIGHTS:
            raise ValueError(f"nights must be 1 to {MAX_NIGHTS}")
        if self.arrival_to < self.arrival_from:
            raise ValueError("arrival_to is before arrival_from")
        if (self.arrival_to - self.arrival_from).days > MAX_ARRIVAL_SPAN_DAYS:
            raise ValueError("arrival range is longer than a year")
        if any(not 0 <= d <= 6 for d in self.arrival_weekdays):
            raise ValueError("arrival_weekdays are 0 (Monday) to 6 (Sunday)")

    @property
    def last_night(self) -> date:
        return self.arrival_to + timedelta(days=self.nights - 1)

    def arrivals(self, today: date | None = None) -> list[date]:
        """Every arrival date that fits, from `today` on."""
        start = max(self.arrival_from, today) if today else self.arrival_from
        days = []
        day = start
        while day <= self.arrival_to:
            if not self.arrival_weekdays or day.weekday() in self.arrival_weekdays:
                days.append(day)
            day += timedelta(days=1)
        return days

    def site_fits(self, site: Campsite) -> bool:
        if self.campsite_ids and site.campsite_id not in self.campsite_ids:
            return False
        if site.type_of_use and site.type_of_use != "Overnight":
            return False
        if site.is_group_site and not self.include_group_sites:
            return False
        if self.group_size and site.max_people and site.max_people < self.group_size:
            return False
        if self.site_types:
            kind = site.campsite_type.upper()
            if not any(word.upper() in kind for word in self.site_types):
                return False
        return True


WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_weekdays(days: list[str | int] | None) -> frozenset[int]:
    """["Fri", "saturday", 6] → {4, 5, 6}. Numbers are 0 = Monday."""
    parsed = set()
    for item in days or ():
        if isinstance(item, int) or str(item).isdigit():
            parsed.add(int(item))
            continue
        key = str(item).strip().lower()[:3]
        if key not in WEEKDAY_NAMES:
            raise ValueError(f"Not a weekday: {item!r}")
        parsed.add(WEEKDAY_NAMES.index(key))
    return frozenset(parsed)


def free_for(site: Campsite, check_in: date, nights: int) -> bool:
    """The arrival night is `Available` and so is every night after it, or
    `Open`. Just past the booking window recreation.gov shows a week of
    `Open` nights: they can't be booked on their own, only as the end of a
    stay that arrives inside the window."""
    if site.nights.get(check_in) != AVAILABLE:
        return False
    return all(
        site.nights.get(check_in + timedelta(days=i)) in (AVAILABLE, OPEN)
        for i in range(1, nights)
    )


def find_stays(
    campground_id: str,
    sites: Iterable[Campsite],
    criteria: StayCriteria,
    today: date | None = None,
) -> list[Stay]:
    """Every (site, arrival) that's bookable for the whole stay, earliest
    arrival first, then by site."""
    arrivals = criteria.arrivals(today)
    stays = [
        Stay(
            campground_id=campground_id,
            campsite_id=site.campsite_id,
            site=site.site,
            loop=site.loop,
            campsite_type=site.campsite_type,
            max_people=site.max_people,
            check_in=check_in,
            nights=criteria.nights,
        )
        for site in sites
        if criteria.site_fits(site)
        for check_in in arrivals
        if free_for(site, check_in, criteria.nights)
    ]
    return sorted(stays, key=lambda s: (s.check_in, _site_key(s.site), s.campsite_id))


def _site_key(site: str) -> tuple[int, str]:
    """Site "9" before "10": numeric where the label is a number."""
    return (int(site), "") if site.isdigit() else (10**9, site)
