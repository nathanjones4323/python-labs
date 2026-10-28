"""ReserveCalifornia (California State Parks): finding campgrounds and what's
open at them, read into recgov's `Campground` and `Campsite` so the rest of
the app treats both sites alike.

Like recreation.gov's, these are the endpoints the website's own pages call
(its script sends them to `calirdr.usedirect.com/rdr/rdr/`, UseDirect's
booking system), with no login:

- `fd/places`: every park, with its name and location
- `fd/facilities`: every facility (a campground, or a loop of one) and the
  park it's in
- `POST search/grid`: each site ("unit") at a facility and a "slice" for
  every night: `IsFree`, `IsWalkin`, `IsBlocked`, `Lock` and `MinStay`

Parks have no search endpoint worth the name (`fd/citypark/namecontains`
is the search box's autocomplete), so the two lists are fetched once and
searched here, by name or distance. Big Sur, say, is Pfeiffer Big Sur by
name, and Andrew Molera or Limekiln by their descriptions or by distance.

A campground id here is `rc-{park}-{facility}` (`rc-690-766`), so it
can't be mistaken for one of recreation.gov's and its page link
(`/park/{park}/{facility}`) comes from the id alone. A night is `Available`
when the site's free, not locked in someone's cart, not walk-in only and
booked online; that's what the website lets you pick.

Only searching and watching: holding (the cart) is recreation.gov's.
"""

import math
import re
import time
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

import httpx

from campsite_booker.recgov import (
    AVAILABLE,
    STATE_PARK_PREFIX,
    USER_AGENT,
    Campground,
    Campsite,
    RecGovError,
    request_json,
)

SITE_URL = "https://www.reservecalifornia.com"
API_URL = "https://calirdr.usedirect.com/rdr/rdr/"
SITE = "ReserveCalifornia"
RESERVED = "Reserved"
WALK_IN = "Walk-in"
NOT_RESERVABLE = "Not Reservable"
# The park and facility lists change a few times a year.
CATALOG_TTL_S = 12 * 3600
# Nights per `search/grid` request (the website asks for a week or two).
GRID_DAYS = 30
# Facilities that aren't overnight camping. The catalog doesn't say, so
# it's by name: day use, parking, tours (Hearst Castle) and picnic areas.
NOT_CAMPING = ("day use", "parking", "tour", "picnic")
# Park name endings stripped to tell whether a facility's name says its park:
# "Pfeiffer Big Sur SP" → "Pfeiffer Big Sur".
PARK_SUFFIX = re.compile(r"\s+(SP|SB|SRA|SHP|SNR|SVRA|SR|SHM|SMR)$", re.IGNORECASE)
EARTH_RADIUS_MILES = 3958.8


class ReserveCaliforniaError(RecGovError):
    """ReserveCalifornia answered with an error or something unreadable. A
    `RecGovError`, so every caller's handling covers it."""


def campground_id(park_id: int | str, facility_id: int | str) -> str:
    return f"{STATE_PARK_PREFIX}{park_id}-{facility_id}"


def split_id(campground_id: str) -> tuple[str, str]:
    """`rc-690-766` → ("690", "766")."""
    park, _, facility = campground_id.removeprefix(STATE_PARK_PREFIX).partition("-")
    if not (park.isdigit() and facility.isdigit()):
        raise ReserveCaliforniaError(
            f"{campground_id!r} isn't a ReserveCalifornia campground id "
            "(rc-park-facility)"
        )
    return park, facility


def facility_url(campground_id: str) -> str:
    park, facility = split_id(campground_id)
    return f"{SITE_URL}/park/{park}/{facility}"


# --- parsing ------------------------------------------------------------------


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # The catalog writes 0 for an unknown location.
    return number or None


def _rows(payload: Any) -> list[dict[str, Any]]:
    """The lists come as a JSON array; tolerate an object keyed by id."""
    if isinstance(payload, dict):
        return list(payload.values())
    return list(payload or [])


def _name(facility: str, park: str) -> str:
    """The facility's name if it says which park, else "Park: Facility"
    ("Pfeiffer Big Sur SP: Campground")."""
    core = PARK_SUFFIX.sub("", park).strip()
    if not park or core.lower() in facility.lower():
        return facility
    return f"{park}: {facility}"


@dataclass(frozen=True)
class Listing:
    """A campground in the catalog, with the text a search matches."""

    campground: Campground
    # Facility and park names.
    names: str
    # Facility and park descriptions.
    description: str


def parse_catalog(places: Any, facilities: Any) -> list[Listing]:
    """Every bookable camping facility, from `fd/places` and `fd/facilities`."""
    parks = {str(p.get("PlaceId")): p for p in _rows(places)}
    listings = []
    for raw in _rows(facilities):
        facility_id, park_id = raw.get("FacilityId"), raw.get("PlaceId")
        park = parks.get(str(park_id))
        if not facility_id or park is None:
            continue
        name = (raw.get("Name") or "").strip()
        if not name or any(word in name.lower() for word in NOT_CAMPING):
            continue
        if raw.get("IsWebViewable") is False or park.get("IsWebViewable") is False:
            continue
        park_name = (park.get("Name") or "").strip()
        latitude = _float(raw.get("Latitude")) or _float(park.get("Latitude"))
        longitude = _float(raw.get("Longitude")) or _float(park.get("Longitude"))
        listings.append(
            Listing(
                Campground(
                    id=campground_id(park_id, facility_id),
                    name=_name(name, park_name),
                    park=park_name or None,
                    state="CA",
                    latitude=latitude,
                    longitude=longitude,
                    reservable=raw.get("AllowWebBooking") is not False
                    and park.get("AllowWebBooking") is not False,
                ),
                names=f"{name} {park_name}".lower(),
                description=_text(
                    f"{raw.get('Description') or ''} {park.get('Description') or ''}"
                ),
            )
        )
    return listings


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).lower().split())


def search_catalog(listings: list[Listing], query: str) -> list[Campground]:
    """Campgrounds whose facility or park names have every word of `query`,
    then those whose descriptions have it as a phrase ("big sur" finds
    Pfeiffer Big Sur, then the parks on the Big Sur coast)."""
    words = query.lower().split()
    if not words:
        return []
    by_name, by_description = [], []
    for listing in listings:
        if all(w in listing.names for w in words):
            by_name.append(listing.campground)
        elif _phrase(query) in listing.description:
            by_description.append(listing.campground)
    return by_name + by_description


def _phrase(query: str) -> str:
    return " ".join(query.lower().split())


def distance_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def near_catalog(
    listings: list[Listing], latitude: float, longitude: float, radius_miles: float
) -> list[Campground]:
    """Campgrounds within `radius_miles`, nearest first, with their distance."""
    found = []
    for listing in listings:
        c = listing.campground
        if c.latitude is None or c.longitude is None:
            continue
        miles = distance_miles(latitude, longitude, c.latitude, c.longitude)
        if miles <= radius_miles:
            found.append(replace(c, distance_miles=round(miles, 1)))
    return sorted(found, key=lambda c: c.distance_miles)


def site_type(unit_name: str) -> str:
    """A unit's name without its number, as recreation.gov writes types:
    "Tent Campsite #023" → "TENT CAMPSITE"."""
    kind = re.sub(r"\s*(#\s*)?\d+[A-Z]?$", "", unit_name.strip(), flags=re.I)
    return " ".join(kind.split()).upper() or "CAMPSITE"


def _status(slice_: dict[str, Any], books_online: bool) -> str:
    if slice_.get("IsWalkin"):
        return WALK_IN
    if not books_online:
        return NOT_RESERVABLE
    if (
        slice_.get("IsFree")
        and slice_.get("Lock") is None
        and not slice_.get("IsBlocked")
    ):
        return AVAILABLE
    return RESERVED


def parse_grid(payload: dict[str, Any]) -> list[Campsite]:
    """Every site in a `search/grid` response, with its nights."""
    facility = payload.get("Facility")
    if not isinstance(facility, dict):
        raise ReserveCaliforniaError(
            f"{SITE} sent no availability: {payload.get('Message') or 'no reason'}"
        )
    sites = []
    for unit in _rows(facility.get("Units")):
        books_online = unit.get("IsWebViewable") is not False and (
            unit.get("AllowWebBooking") is not False
        )
        nights, min_nights = {}, {}
        for key, slice_ in (unit.get("Slices") or {}).items():
            # "2026-11-06T00:00:00": a park-local date.
            night = date.fromisoformat(str(slice_.get("Date") or key)[:10])
            nights[night] = _status(slice_, books_online)
            if int(slice_.get("MinStay") or 1) > 1:
                min_nights[night] = int(slice_["MinStay"])
        name = (unit.get("Name") or "").strip()
        unit_id = str(unit["UnitId"])
        sites.append(
            Campsite(
                campsite_id=unit_id,
                site=(unit.get("ShortName") or "").strip() or name or unit_id,
                loop="",
                campsite_type=site_type(name),
                type_of_use="Overnight",
                max_people=int(unit.get("MaxOccupancy") or 0),
                min_people=0,
                nights=nights,
                min_nights=min_nights,
            )
        )
    return sites


def windows(first: date, last: date, days: int = GRID_DAYS) -> list[tuple[date, date]]:
    """`first` to `last` in spans of at most `days` nights."""
    spans = []
    start = first
    while start <= last:
        end = min(start + timedelta(days=days - 1), last)
        spans.append((start, end))
        start = end + timedelta(days=1)
    return spans


def _api_date(day: date) -> str:
    return day.strftime("%m-%d-%Y")


# --- the client -----------------------------------------------------------------


class ReserveCalifornia:
    """A small synchronous client, the same shape as `RecGov`. Use as a
    context manager, or `close()`."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            base_url=API_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Origin": SITE_URL,
                "Referer": f"{SITE_URL}/",
            },
            timeout=20,
            follow_redirects=True,
        )
        self._catalog: list[Listing] | None = None
        self._catalog_at = 0.0

    def __enter__(self) -> "ReserveCalifornia":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        return request_json(
            self._client, method, path, SITE, ReserveCaliforniaError, **kwargs
        )

    def catalog(self) -> list[Listing]:
        if self._catalog is None or time.monotonic() - self._catalog_at > CATALOG_TTL_S:
            self._catalog = parse_catalog(
                self._request("GET", "fd/places"),
                self._request("GET", "fd/facilities"),
            )
            self._catalog_at = time.monotonic()
        return self._catalog

    def search(self, query: str, limit: int = 20) -> list[Campground]:
        """Campgrounds whose name, park or description matches `query`."""
        return search_catalog(self.catalog(), query)[:limit]

    def search_near(
        self, latitude: float, longitude: float, radius_miles: float, limit: int = 30
    ) -> list[Campground]:
        """Campgrounds within `radius_miles`, nearest first."""
        return near_catalog(self.catalog(), latitude, longitude, radius_miles)[:limit]

    def campground(self, campground_id: str) -> Campground:
        split_id(campground_id)
        for listing in self.catalog():
            if listing.campground.id == campground_id:
                return listing.campground
        raise ReserveCaliforniaError(f"No campground {campground_id} on {SITE}")

    def availability(
        self, campground_id: str, first_night: date, last_night: date
    ) -> dict[str, Campsite]:
        """Every site's status for each night from `first_night` to
        `last_night`, a request per `GRID_DAYS` nights."""
        _, facility = split_id(campground_id)
        merged: dict[str, Campsite] = {}
        for start, end in windows(first_night, last_night):
            payload = self._request(
                "POST",
                "search/grid",
                json={
                    "FacilityId": facility,
                    "StartDate": _api_date(start),
                    "EndDate": _api_date(end),
                    "UnitSort": "orderby",
                    "InSeasonOnly": True,
                    "WebOnly": True,
                    "IsADA": False,
                    "RestrictADA": False,
                    "UnitCategoryId": 0,
                    "SleepingUnitId": 0,
                    "MinVehicleLength": 0,
                    "UnitTypesGroupIds": [],
                },
            )
            for site in parse_grid(payload):
                if site.campsite_id in merged:
                    merged[site.campsite_id].nights.update(site.nights)
                    merged[site.campsite_id].min_nights.update(site.min_nights)
                else:
                    merged[site.campsite_id] = site
        return merged
