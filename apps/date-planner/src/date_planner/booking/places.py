"""A venue's Google Place ID, from Places API (New) Text Search.

The Resy connector's availability widget identifies a restaurant by its
Google Place ID and nothing else, and it refuses a guessed one. In claude.ai
chat Claude has a places search of its own to supply it; Claude Code has
none, so the server looks it up here and keeps it on the venue row.

One `places:searchText` call per venue, asking only for the id, name,
address and location (billed as Text Search Pro, which has a monthly free
allowance far above a couple's dinners), and the id is kept, so a venue is
looked up once. A candidate counts as the venue only when its name
matches and, if the venue has coordinates, it sits within `MATCH_RADIUS_M`
of them — two restaurants of the same name across town don't.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import httpx

from date_planner.config import get_settings
from date_planner.errors import PlacesFailed
from date_planner.planning.travel import LatLng, great_circle_m

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.location"
# Search near the venue, not only inside this circle: a bias, not a filter.
BIAS_RADIUS_M = 5_000.0
# A pin dropped from the venue's own site and Google's can disagree by a
# car park's width; a different branch is further than this.
MATCH_RADIUS_M = 300.0
MAX_RESULTS = 5


@dataclass(frozen=True)
class Place:
    place_id: str
    name: str
    address: str | None
    location: LatLng | None


@dataclass(frozen=True)
class Candidate:
    place: Place
    # From the venue's own coordinates; None when either side has none.
    distance_m: float | None
    name_matches: bool

    @property
    def is_match(self) -> bool:
        if not self.name_matches:
            return False
        return self.distance_m is None or self.distance_m <= MATCH_RADIUS_M


_FILLER = {"the", "restaurant", "bar", "and", "cafe", "kitchen"}


def _words(name: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    words = set(re.findall(r"[a-z0-9]+", plain.lower().replace("&", " and ")))
    return (words - _FILLER) or words


def names_match(ours: str, theirs: str) -> bool:
    """Whether Google's name is plausibly our venue's.

    Every significant word of the shorter name has to appear in the longer:
    "Nobu" is "Nobu Malibu", "A Tí" is "A TÍ Restaurant", but "Bestia" is
    not "Bavel".
    """
    a, b = _words(ours), _words(theirs)
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return shorter <= longer


def _parse(raw: dict[str, Any]) -> Place | None:
    place_id = raw.get("id")
    if not isinstance(place_id, str) or not place_id:
        return None
    location = raw.get("location") or {}
    lat, lng = location.get("latitude"), location.get("longitude")
    return Place(
        place_id=place_id,
        name=(raw.get("displayName") or {}).get("text") or "",
        address=raw.get("formattedAddress"),
        location=LatLng(lat, lng) if lat is not None and lng is not None else None,
    )


async def search(
    query: str,
    api_key: str,
    near: LatLng | None = None,
) -> list[Place]:
    """Google's best few places for `query`, biased towards `near`."""
    body: dict[str, Any] = {"textQuery": query, "pageSize": MAX_RESULTS}
    if near is not None:
        body["locationBias"] = {
            "circle": {
                "center": {"latitude": near.lat, "longitude": near.lng},
                "radius": BIAS_RADIUS_M,
            }
        }
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK}
    try:
        async with httpx.AsyncClient(timeout=get_settings().fetch_timeout) as client:
            response = await client.post(SEARCH_URL, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise PlacesFailed(f"Google Places request failed: {exc}") from exc
    if response.status_code != 200:
        # Google puts the reason (bad key, API not enabled, quota) in the body.
        try:
            reason = response.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            reason = response.text[:200]
        raise PlacesFailed(f"Google Places said {response.status_code}: {reason}")
    try:
        data = response.json()
    except ValueError as exc:
        raise PlacesFailed("Google Places returned something other than JSON") from exc
    raw_places = data.get("places", []) if isinstance(data, dict) else []
    return [place for raw in raw_places if (place := _parse(raw)) is not None]


def rank(name: str, near: LatLng | None, places: list[Place]) -> list[Candidate]:
    """`places` as candidates for the venue `name` at `near`, Google's order
    kept."""
    return [
        Candidate(
            place=place,
            distance_m=(
                great_circle_m(near, place.location)
                if near is not None and place.location is not None
                else None
            ),
            name_matches=names_match(name, place.name),
        )
        for place in places
    ]
