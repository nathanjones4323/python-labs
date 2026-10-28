"""recreation.gov and ReserveCalifornia behind one client.

`Campgrounds` has `RecGov`'s methods and sends each call to the site the
campground id belongs to: `rc-…` ids are ReserveCalifornia's (California
state parks), the rest recreation.gov's. Searches ask both and put the
results together; one site failing leaves the other's.
"""

from datetime import date
from typing import Any

from loguru import logger

from campsite_booker.recgov import (
    Campground,
    Campsite,
    RecGov,
    RecGovError,
    is_state_park,
)
from campsite_booker.reservecalifornia import ReserveCalifornia


class Campgrounds:
    def __init__(
        self,
        recgov: RecGov | None = None,
        state_parks: ReserveCalifornia | None = None,
    ) -> None:
        self.recgov = recgov or RecGov()
        self.state_parks = state_parks or ReserveCalifornia()

    def __enter__(self) -> "Campgrounds":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.recgov.close()
        self.state_parks.close()

    def _site(self, campground_id: str) -> RecGov | ReserveCalifornia:
        return self.state_parks if is_state_park(campground_id) else self.recgov

    def search(self, query: str, limit: int = 20) -> list[Campground]:
        """Campgrounds matching `query` on both sites: state parks first, as
        their matches are by name (recreation.gov's search is looser)."""
        return _both(
            lambda: self.state_parks.search(query, limit),
            lambda: self.recgov.search(query, limit),
        )[:limit]

    def search_near(
        self, latitude: float, longitude: float, radius_miles: float, limit: int = 30
    ) -> list[Campground]:
        """Campgrounds on both sites within `radius_miles`, nearest first."""
        found = _both(
            lambda: self.state_parks.search_near(
                latitude, longitude, radius_miles, limit
            ),
            lambda: self.recgov.search_near(latitude, longitude, radius_miles, limit),
        )
        return sorted(
            found,
            key=lambda c: c.distance_miles if c.distance_miles is not None else 1e9,
        )[:limit]

    def campground(self, campground_id: str) -> Campground:
        return self._site(campground_id).campground(campground_id)

    def availability(
        self, campground_id: str, first_night: date, last_night: date
    ) -> dict[str, Campsite]:
        return self._site(campground_id).availability(
            campground_id, first_night, last_night
        )

    def campground_of(self, campsite_id: str) -> str:
        """For recreation.gov's alert emails, so recreation.gov's sites."""
        return self.recgov.campground_of(campsite_id)

    def releases(self, campground_id: str) -> dict[str, Any]:
        """recreation.gov's release schedule. ReserveCalifornia publishes
        none (each day opens six months ahead, at 8 AM), so a state park
        has no release to arm for and is just checked as usual."""
        if is_state_park(campground_id):
            return {}
        return self.recgov.releases(campground_id)


def _both(*searches) -> list[Campground]:
    found, errors = [], []
    for search in searches:
        try:
            found += search()
        except RecGovError as e:
            logger.warning(f"Search failed: {e}")
            errors.append(e)
    if len(errors) == len(searches):
        raise errors[0]
    return found
