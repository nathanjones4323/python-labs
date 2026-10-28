"""recreation.gov's own JSON API: finding campgrounds and what's open at them.

None of this needs a login. These are the endpoints the website's pages call
(seen in the browser's network log), so they're unversioned and could change;
the parsing is kept in module-level functions so it's testable against saved
responses in `tests/fixtures` without the network.

- `/api/search?q=&fq=entity_type:campground`: campgrounds matching some text
- `/api/search/geo?lat=&lng=&radius=`: campgrounds within `radius` miles
- `/api/camps/campgrounds/{id}`: one campground's name and rules
- `/api/camps/campsites/{id}`: one site, and the campground it's in
- `/api/camps/availability/campground/{id}/month?start_date=`: each site's
  status for every night of one month
- `/api/camps/campgrounds/{id}/releases`: when the next dates open, and up
  to which arrival date (see `releases.py`)

A night's status is a word: `Available` can be booked now. Just past the
booking window come a week of `Open` nights (bookable only as the end of a
stay that arrives inside the window), then `NYR` (not yet released). The
rest are `Reserved`, `Closed`, `Not Reservable`, `Lottery` and the like.
"""

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx
from loguru import logger

BASE_URL = "https://www.recreation.gov"
# The API answers a script's default user agent with 403s now and then.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
AVAILABLE = "Available"
# Past the booking window: bookable only as the tail of a stay that arrives
# inside it (or, where every night is Open, first come first served).
OPEN = "Open"
# Not yet released.
NYR = "NYR"
# Retries for a 429 / 5xx / dropped connection, with this many seconds
# between the first two (doubling after).
ATTEMPTS = 3
BACKOFF_S = 2.0


class RecGovError(RuntimeError):
    """recreation.gov answered with an error or something unreadable."""


@dataclass(frozen=True)
class Campground:
    id: str
    name: str
    park: str | None = None
    city: str | None = None
    state: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    # From the search's centre, when it was a geo search.
    distance_miles: float | None = None
    price_min: float | None = None
    price_max: float | None = None
    reservable: bool = True

    @property
    def url(self) -> str:
        return campground_url(self.id)


@dataclass
class Campsite:
    """One site at a campground, and its status for each night fetched."""

    campsite_id: str
    site: str
    loop: str
    campsite_type: str
    type_of_use: str
    max_people: int
    min_people: int
    nights: dict[date, str] = field(default_factory=dict)

    @property
    def is_group_site(self) -> bool:
        return "GROUP" in self.campsite_type.upper()


@dataclass(frozen=True)
class Stay:
    """A site free for `nights` nights in a row, from `check_in`."""

    campground_id: str
    campsite_id: str
    site: str
    loop: str
    campsite_type: str
    max_people: int
    check_in: date
    nights: int

    @property
    def check_out(self) -> date:
        return self.check_in + timedelta(days=self.nights)

    @property
    def url(self) -> str:
        return campsite_url(self.campsite_id)


def campground_url(campground_id: str) -> str:
    return f"{BASE_URL}/camping/campgrounds/{campground_id}"


def campsite_url(campsite_id: str) -> str:
    return f"{BASE_URL}/camping/campsites/{campsite_id}"


CART_URL = f"{BASE_URL}/cart"
RESERVATIONS_URL = f"{BASE_URL}/account/reservations"


def display_name(name: str) -> str:
    """recreation.gov stores many names in capitals ("SANTA CRUZ ISLAND
    SCORPION"); its pages show them in title case, so do we."""
    return name.title() if name.isupper() else name


# --- parsing ------------------------------------------------------------------


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_search(payload: dict[str, Any]) -> list[Campground]:
    """Campgrounds from a `/api/search` or `/api/search/geo` response."""
    found = []
    for result in payload.get("results") or []:
        if result.get("entity_type") != "campground":
            continue
        price = result.get("price_range") or {}
        found.append(
            Campground(
                id=str(result["entity_id"]),
                name=display_name(result.get("name") or ""),
                park=result.get("parent_name"),
                city=result.get("city"),
                state=result.get("state_code"),
                latitude=_float(result.get("latitude")),
                longitude=_float(result.get("longitude")),
                distance_miles=_float(result.get("distance")),
                price_min=_float(price.get("amount_min")),
                price_max=_float(price.get("amount_max")),
                reservable=bool(result.get("reservable", True)),
            )
        )
    return found


def parse_month(payload: dict[str, Any]) -> list[Campsite]:
    """Every site in a month-availability response, with its nights."""
    sites = []
    for raw in (payload.get("campsites") or {}).values():
        nights = {
            # "2026-11-01T00:00:00Z": a campground-local date, not an instant.
            date.fromisoformat(key[:10]): status
            for key, status in (raw.get("availabilities") or {}).items()
        }
        sites.append(
            Campsite(
                campsite_id=str(raw["campsite_id"]),
                site=str(raw.get("site") or raw["campsite_id"]),
                loop=raw.get("loop") or "",
                campsite_type=raw.get("campsite_type") or "",
                type_of_use=raw.get("type_of_use") or "",
                max_people=int(raw.get("max_num_people") or 0),
                min_people=int(raw.get("min_num_people") or 0),
                nights=nights,
            )
        )
    return sites


def merge_months(months: list[list[Campsite]]) -> dict[str, Campsite]:
    """One `Campsite` per site, its nights from every month fetched."""
    merged: dict[str, Campsite] = {}
    for month in months:
        for site in month:
            if site.campsite_id in merged:
                merged[site.campsite_id].nights.update(site.nights)
            else:
                merged[site.campsite_id] = site
    return merged


def months_between(start: date, end: date) -> list[date]:
    """The first of each month from `start`'s through `end`'s."""
    months = []
    month = start.replace(day=1)
    while month <= end:
        months.append(month)
        month = (month + timedelta(days=32)).replace(day=1)
    return months


# --- the client -----------------------------------------------------------------


class RecGov:
    """A small synchronous client. Use as a context manager, or `close()`."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=20,
            follow_redirects=True,
        )

    def __enter__(self) -> "RecGov":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any] | list | None = None) -> Any:
        wait = BACKOFF_S
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError as e:
                if attempt == ATTEMPTS:
                    raise RecGovError(f"Couldn't reach recreation.gov: {e}") from e
            else:
                if response.status_code < 400:
                    try:
                        return response.json()
                    except ValueError as e:
                        raise RecGovError(f"{path} didn't return JSON") from e
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable or attempt == ATTEMPTS:
                    raise RecGovError(
                        f"recreation.gov answered {response.status_code} for {path}"
                    )
            logger.debug(f"Retrying {path} in {wait:.0f}s (attempt {attempt})")
            time.sleep(wait)
            wait *= 2
        raise AssertionError("unreachable")

    def search(self, query: str, limit: int = 20) -> list[Campground]:
        """Campgrounds whose name, park or place matches `query`."""
        payload = self._get(
            "/api/search",
            {"q": query, "fq": "entity_type:campground", "size": limit},
        )
        return parse_search(payload)

    def search_near(
        self, latitude: float, longitude: float, radius_miles: float, limit: int = 30
    ) -> list[Campground]:
        """Campgrounds within `radius_miles`, nearest first."""
        payload = self._get(
            "/api/search/geo",
            {
                "lat": latitude,
                "lng": longitude,
                "radius": radius_miles,
                "fq": "entity_type:campground",
                "size": limit,
            },
        )
        return sorted(
            parse_search(payload),
            key=lambda c: c.distance_miles if c.distance_miles is not None else 1e9,
        )

    def campground(self, campground_id: str) -> Campground:
        payload = self._get(f"/api/camps/campgrounds/{campground_id}")
        raw = payload.get("campground") or {}
        if not raw.get("facility_name"):
            raise RecGovError(f"No campground {campground_id} on recreation.gov")
        address = (raw.get("addresses") or [{}])[0]
        return Campground(
            id=str(raw.get("facility_id") or campground_id),
            name=display_name(raw["facility_name"]),
            city=address.get("city") or raw.get("city"),
            state=address.get("state_code"),
            latitude=_float(raw.get("facility_latitude")),
            longitude=_float(raw.get("facility_longitude")),
        )

    def month(self, campground_id: str, month: date) -> list[Campsite]:
        payload = self._get(
            f"/api/camps/availability/campground/{campground_id}/month",
            {"start_date": f"{month.replace(day=1).isoformat()}T00:00:00.000Z"},
        )
        return parse_month(payload)

    def campground_of(self, campsite_id: str) -> str:
        """The campground a site belongs to."""
        payload = self._get(f"/api/camps/campsites/{campsite_id}")
        facility = (payload.get("campsite") or {}).get("facility_id")
        if not facility:
            raise RecGovError(f"No campsite {campsite_id} on recreation.gov")
        return str(facility)

    def releases(self, campground_id: str) -> dict[str, Any]:
        """The raw release schedule; `releases.parse_releases` reads it."""
        return self._get(f"/api/camps/campgrounds/{campground_id}/releases")

    def availability(
        self, campground_id: str, first_night: date, last_night: date
    ) -> dict[str, Campsite]:
        """Every site's status for each night from `first_night` to
        `last_night`, one request per calendar month."""
        return merge_months(
            [
                self.month(campground_id, month)
                for month in months_between(first_night, last_night)
            ]
        )
