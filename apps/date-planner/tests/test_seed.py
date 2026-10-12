"""The seed writes the Places snapshot, and only falls back to the API without one."""

import json

import httpx
import pytest
import respx
from date_planner.db import repo
from date_planner.seed import PLACES, seed
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Factory = async_sessionmaker[AsyncSession]
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"

# What a fake Google answers when a venue has no snapshot. Coordinates are the
# ones in seed.py, so the answer sits on top of them and its name matches.
LIVE = {
    "Nobu Malibu": ("ChIJ-nobu", 34.0389596, -118.6696041),
    "Solstice Canyon": ("ChIJ-solstice", 34.03765, -118.7477),
    "Getty Villa": ("ChIJ-getty", 34.0449819, -118.5650197),
}


def _answer(request: httpx.Request) -> httpx.Response:
    query = json.loads(request.content)["textQuery"]
    name = next(n for n in LIVE if query.startswith(n))
    place_id, lat, lng = LIVE[name]
    return httpx.Response(
        200,
        json={
            "places": [
                {
                    "id": place_id,
                    "displayName": {"text": name},
                    "formattedAddress": f"{name}, CA, USA",
                    "location": {"latitude": lat, "longitude": lng},
                }
            ]
        },
    )


async def test_seed_stores_the_snapshot_without_calling_google(
    db: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k3y")
    with respx.mock(assert_all_called=False) as router:
        route = router.post(PLACES_URL)
        await seed()
        await seed()
    assert route.call_count == 0

    async with db() as session:
        venues = {v.name: v for v in await repo.list_venues(session)}
    for (name, _), data in PLACES.items():
        assert venues[name].google_place_id == data["id"]
        assert venues[name].google_place_data == data


async def test_seed_looks_up_a_venue_with_no_snapshot_once(
    db: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k3y")
    monkeypatch.setattr("date_planner.seed.PLACES", {})
    with respx.mock() as router:
        route = router.post(PLACES_URL).mock(side_effect=_answer)
        await seed()
        assert route.call_count == 3
        await seed()
        assert route.call_count == 3  # the venue rows were the cache

    async with db() as session:
        venues = {v.name: v for v in await repo.list_venues(session)}
    for name, (place_id, _, _) in LIVE.items():
        data = venues[name].google_place_data
        assert venues[name].google_place_id == place_id
        assert data is not None and data["id"] == place_id


async def test_seed_survives_a_places_failure(
    db: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k3y")
    monkeypatch.setattr("date_planner.seed.PLACES", {})
    with respx.mock() as router:
        router.post(PLACES_URL).respond(403, json={"error": {"message": "denied"}})
        await seed()
    async with db() as session:
        venues = await repo.list_venues(session)
    assert len(venues) == 3
    assert all(v.google_place_id is None for v in venues)


async def test_seed_without_a_key_or_snapshot_makes_no_calls(
    db: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "")
    monkeypatch.setattr("date_planner.seed.PLACES", {})
    with respx.mock(assert_all_called=False) as router:
        route = router.post(PLACES_URL)
        await seed()
    assert route.call_count == 0
