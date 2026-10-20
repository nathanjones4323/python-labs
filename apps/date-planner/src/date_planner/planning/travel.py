"""How long a leg between two venues takes, behind a swappable provider.

`TRAVEL_PROVIDER` picks one:

* `haversine` (default) — free and offline: great-circle distance × 1.4 for
  roads that don't run straight, at 40 km/h driving or 4.8 km/h walking. A
  coarse estimate, and says so (`is_estimate`). On the coast it runs
  pessimistic: PCH is faster than 40 km/h for most of its length.
* `osrm` — a self-hosted OSRM (`OSRM_URL`; see docker-compose.yml's
  `routing` profile). Real road routing over OpenStreetMap.
* `ors` — OpenRouteService's hosted API, with `ORS_API_KEY`.
* `google` — a placeholder for traffic-aware times later; raises.

Neither OSM provider knows about traffic, so every driving time gets
`TRAVEL_PADDING_PCT` added (20% by default), whichever provider timed it.
What's cached is the provider's raw answer; padding goes on as it's read, so
changing the knob takes effect without clearing the cache.
"""

import math
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from date_planner.config import Settings
from date_planner.db import repo
from date_planner.db.models import TravelMode, Venue
from date_planner.errors import MissingCoordinates, TravelFailed
from date_planner.json_api import request_json

ROAD_FACTOR = 1.4
SPEED_KMH = {TravelMode.DRIVING: 40.0, TravelMode.WALKING: 4.8}
EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class LatLng:
    lat: float
    lng: float


@dataclass(frozen=True)
class Route:
    minutes: float
    meters: float


@dataclass(frozen=True)
class Leg:
    """A leg as a tool reports it: padded, rounded up to a whole minute."""

    minutes: int
    meters: int
    provider: str
    is_estimate: bool
    from_cache: bool = False


class TravelProvider(Protocol):
    name: str
    # True when the answer is arithmetic rather than a road route.
    is_estimate: bool

    async def route(self, origin: LatLng, dest: LatLng, mode: TravelMode) -> Route: ...


def great_circle_m(a: LatLng, b: LatLng) -> float:
    """Haversine distance in metres."""
    phi1, phi2 = math.radians(a.lat), math.radians(b.lat)
    dphi = phi2 - phi1
    dlmb = math.radians(b.lng - a.lng)
    h = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


class HaversineProvider:
    name = "haversine"
    is_estimate = True

    async def route(self, origin: LatLng, dest: LatLng, mode: TravelMode) -> Route:
        meters = great_circle_m(origin, dest) * ROAD_FACTOR
        minutes = meters / 1000 / SPEED_KMH[mode] * 60
        return Route(minutes=minutes, meters=meters)


class OsrmProvider:
    """`GET {OSRM_URL}/route/v1/{profile}/{lng},{lat};{lng},{lat}?overview=false`.

    A local osrm-routed serves whichever profile it was built with; the
    profile segment in the URL is what the public demo uses to tell them
    apart.
    """

    name = "osrm"
    is_estimate = False
    PROFILES = {TravelMode.DRIVING: "driving", TravelMode.WALKING: "foot"}

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client

    async def route(self, origin: LatLng, dest: LatLng, mode: TravelMode) -> Route:
        url = (
            f"{self.base_url}/route/v1/{self.PROFILES[mode]}/"
            f"{origin.lng},{origin.lat};{dest.lng},{dest.lat}"
        )
        data = await _get_json(self.client, "GET", url, params={"overview": "false"})
        if data.get("code") != "Ok" or not data.get("routes"):
            raise TravelFailed(f"OSRM found no route: {data.get('code')}")
        best = data["routes"][0]
        return Route(minutes=best["duration"] / 60, meters=best["distance"])


class OrsProvider:
    """OpenRouteService directions, `POST /v2/directions/{profile}`."""

    name = "ors"
    is_estimate = False
    BASE_URL = "https://api.openrouteservice.org/v2/directions"
    PROFILES = {TravelMode.DRIVING: "driving-car", TravelMode.WALKING: "foot-walking"}

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.client = client

    async def route(self, origin: LatLng, dest: LatLng, mode: TravelMode) -> Route:
        data = await _get_json(
            self.client,
            "POST",
            f"{self.BASE_URL}/{self.PROFILES[mode]}",
            json={"coordinates": [[origin.lng, origin.lat], [dest.lng, dest.lat]]},
            headers={"Authorization": self.api_key},
        )
        try:
            summary = data["routes"][0]["summary"]
            return Route(minutes=summary["duration"] / 60, meters=summary["distance"])
        except (KeyError, IndexError) as exc:
            raise TravelFailed("OpenRouteService returned no route") from exc


class GoogleProvider:
    """Google Routes, for traffic-aware times. Deliberately not built yet."""

    name = "google"
    is_estimate = False

    async def route(self, origin: LatLng, dest: LatLng, mode: TravelMode) -> Route:
        raise TravelFailed("The Google Routes provider isn't implemented yet")


async def _get_json(
    client: httpx.AsyncClient | None, method: str, url: str, **kwargs: Any
) -> dict[str, Any]:
    data = await request_json(
        client,
        method,
        url,
        failure=TravelFailed,
        service="Routing",
        reason=_routing_reason,
        **kwargs,
    )
    if not isinstance(data, dict):
        raise TravelFailed("Routing returned something other than a JSON object")
    return data


def _routing_reason(body: Any) -> str | None:
    """The explanation in an error body: OSRM's `message`, or ORS's
    `error.message` (or a bare `error` string)."""
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict):
        error = error.get("message")
    detail = error if isinstance(error, str) else body.get("message")
    return detail if isinstance(detail, str) else None


def make_provider(
    settings: Settings, client: httpx.AsyncClient | None = None
) -> TravelProvider:
    match settings.travel_provider:
        case "haversine":
            return HaversineProvider()
        case "osrm":
            return OsrmProvider(settings.osrm_url, client)
        case "ors":
            if not settings.ors_api_key:
                raise TravelFailed("TRAVEL_PROVIDER=ors needs ORS_API_KEY")
            return OrsProvider(settings.ors_api_key, client)
        case "google":
            return GoogleProvider()
    raise TravelFailed(f"Unknown TRAVEL_PROVIDER {settings.travel_provider!r}")


def coordinates(venue: Venue) -> LatLng:
    if venue.lat is None or venue.lng is None:
        raise MissingCoordinates(
            f"{venue.name} ({venue.id}) has no lat/lng; look them up and save "
            "them with upsert_venue."
        )
    return LatLng(venue.lat, venue.lng)


def padded(route: Route, mode: TravelMode, padding_pct: float) -> Route:
    if mode is not TravelMode.DRIVING:
        return route
    return Route(minutes=route.minutes * (1 + padding_pct / 100), meters=route.meters)


def to_leg(route: Route, provider: TravelProvider, *, from_cache: bool = False) -> Leg:
    return Leg(
        minutes=math.ceil(round(route.minutes, 6)),
        meters=round(route.meters),
        provider=provider.name,
        is_estimate=provider.is_estimate,
        from_cache=from_cache,
    )


async def estimate(
    session: AsyncSession,
    origin: Venue,
    dest: Venue,
    mode: TravelMode,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> Leg:
    """The leg from `origin` to `dest`: cache first, then the provider.

    Raises `MissingCoordinates` if either end has no lat/lng.
    """
    provider = make_provider(settings, client)
    start, end = coordinates(origin), coordinates(dest)
    if origin.id == dest.id:
        return Leg(0, 0, provider.name, provider.is_estimate)
    cached = await repo.cached_travel(
        session,
        origin.id,
        dest.id,
        mode,
        provider.name,
        ttl_days=settings.travel_cache_ttl_days,
    )
    if cached is not None:
        raw = Route(minutes=cached.minutes, meters=cached.meters)
        return to_leg(
            padded(raw, mode, settings.travel_padding_pct), provider, from_cache=True
        )
    raw = await provider.route(start, end, mode)
    await repo.cache_travel(
        session,
        origin.id,
        dest.id,
        mode,
        provider.name,
        minutes=raw.minutes,
        meters=raw.meters,
    )
    return to_leg(padded(raw, mode, settings.travel_padding_pct), provider)
