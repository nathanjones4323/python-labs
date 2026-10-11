"""Sunset and travel: astral against a published value, the haversine
arithmetic, OSRM / ORS response parsing (mocked), padding, and the cache."""

import math
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from date_planner.config import Settings
from date_planner.db import repo
from date_planner.db.models import TravelMode
from date_planner.errors import MissingCoordinates, TravelFailed
from date_planner.planning import sunset, travel
from date_planner.planning.travel import LatLng, Route
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LA = ZoneInfo("America/Los_Angeles")
NOBU = LatLng(34.0389596, -118.6696041)
GETTY = LatLng(34.0449819, -118.5650197)
OSRM = "http://osrm.test"


def settings(**overrides: Any) -> Settings:
    return Settings.model_validate(overrides)


# --- sunset ----------------------------------------------------------------


def test_sunset_matches_the_naval_observatory() -> None:
    # aa.usno.navy.mil for 34.0390 N, 118.6696 W on 2026-10-24, UTC-7:
    # set 18:10, end of civil twilight 18:35.
    result = sunset.compute(NOBU.lat, NOBU.lng, date(2026, 10, 24), LA)
    assert abs(result.sunset - datetime(2026, 10, 24, 18, 10, tzinfo=LA)).seconds <= 60
    assert (
        abs(result.civil_dusk - datetime(2026, 10, 24, 18, 35, tzinfo=LA)).seconds <= 60
    )
    assert result.golden_hour_start < result.sunset < result.civil_dusk
    assert result.sunset.tzinfo == LA


# --- providers -------------------------------------------------------------


def test_great_circle_distance() -> None:
    # One degree of latitude on a 6371 km sphere.
    assert travel.great_circle_m(LatLng(0, 0), LatLng(1, 0)) == pytest.approx(
        111_194.9, abs=1
    )
    assert travel.great_circle_m(GETTY, GETTY) == 0


async def test_haversine_is_road_factor_at_city_speeds() -> None:
    provider = travel.HaversineProvider()
    straight = travel.great_circle_m(NOBU, GETTY)
    driving = await provider.route(NOBU, GETTY, TravelMode.DRIVING)
    walking = await provider.route(NOBU, GETTY, TravelMode.WALKING)
    assert driving.meters == pytest.approx(straight * 1.4)
    assert driving.minutes == pytest.approx(straight * 1.4 / 1000 / 40 * 60)
    assert walking.minutes == pytest.approx(straight * 1.4 / 1000 / 4.8 * 60)
    assert provider.is_estimate


@respx.mock
async def test_osrm_response_is_parsed() -> None:
    route = respx.get(
        f"{OSRM}/route/v1/driving/{NOBU.lng},{NOBU.lat};{GETTY.lng},{GETTY.lat}"
    ).respond(
        200,
        json={
            "code": "Ok",
            "routes": [{"duration": 840.0, "distance": 10250.5, "legs": []}],
            "waypoints": [],
        },
    )
    result = await travel.OsrmProvider(OSRM).route(NOBU, GETTY, TravelMode.DRIVING)
    assert route.called
    assert route.calls.last.request.url.params["overview"] == "false"
    assert result == Route(minutes=14.0, meters=10250.5)


@respx.mock
async def test_osrm_walks_with_the_foot_profile() -> None:
    route = respx.get(url__startswith=f"{OSRM}/route/v1/foot/").respond(
        200, json={"code": "Ok", "routes": [{"duration": 600, "distance": 800}]}
    )
    await travel.OsrmProvider(OSRM).route(NOBU, GETTY, TravelMode.WALKING)
    assert route.called


@respx.mock
async def test_osrm_no_route_is_a_travel_failure() -> None:
    respx.get(url__startswith=OSRM).respond(200, json={"code": "NoRoute", "routes": []})
    with pytest.raises(TravelFailed, match="NoRoute"):
        await travel.OsrmProvider(OSRM).route(NOBU, GETTY, TravelMode.DRIVING)


@respx.mock
async def test_osrm_unreachable_is_a_travel_failure() -> None:
    respx.get(url__startswith=OSRM).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(TravelFailed, match="failed"):
        await travel.OsrmProvider(OSRM).route(NOBU, GETTY, TravelMode.DRIVING)


@respx.mock
async def test_ors_response_is_parsed() -> None:
    route = respx.post(
        "https://api.openrouteservice.org/v2/directions/driving-car"
    ).respond(200, json={"routes": [{"summary": {"distance": 9000, "duration": 720}}]})
    result = await travel.OrsProvider("key-123").route(NOBU, GETTY, TravelMode.DRIVING)
    assert result == Route(minutes=12.0, meters=9000)
    request = route.calls.last.request
    assert request.headers["Authorization"] == "key-123"


async def test_google_is_a_stub() -> None:
    with pytest.raises(NotImplementedError):
        await travel.GoogleProvider().route(NOBU, GETTY, TravelMode.DRIVING)


def test_the_provider_comes_from_config() -> None:
    assert isinstance(travel.make_provider(settings()), travel.HaversineProvider)
    osrm = travel.make_provider(settings(TRAVEL_PROVIDER="osrm", OSRM_URL=OSRM))
    assert isinstance(osrm, travel.OsrmProvider) and osrm.base_url == OSRM
    with pytest.raises(TravelFailed, match="ORS_API_KEY"):
        travel.make_provider(settings(TRAVEL_PROVIDER="ors"))


def test_padding_is_for_driving_only() -> None:
    raw = Route(minutes=30, meters=20_000)
    assert travel.padded(raw, TravelMode.DRIVING, 20).minutes == pytest.approx(36)
    assert travel.padded(raw, TravelMode.WALKING, 20) == raw


def test_legs_round_up_to_the_minute() -> None:
    provider = travel.HaversineProvider()
    assert travel.to_leg(Route(12.01, 9000.4), provider).minutes == 13
    # Float noise doesn't tip a whole minute over.
    assert travel.to_leg(Route(36.000000001, 1), provider).minutes == 36


# --- with the cache --------------------------------------------------------


async def test_estimate_caches_unpadded_and_pads_on_read(
    db: async_sessionmaker[AsyncSession], seeded: dict[str, Any]
) -> None:
    config = settings(TRAVEL_PROVIDER="osrm", OSRM_URL=OSRM)
    with respx.mock() as router:
        osrm = router.get(url__startswith=OSRM).respond(
            200, json={"code": "Ok", "routes": [{"duration": 1200, "distance": 9000}]}
        )
        async with db() as session, session.begin():
            getty = await repo.get_venue(session, seeded["getty_id"])
            nobu = await repo.get_venue(session, seeded["nobu_id"])
            first = await travel.estimate(
                session, getty, nobu, TravelMode.DRIVING, config
            )
            assert (first.minutes, first.from_cache, first.is_estimate) == (
                24,  # 20 min + 20%
                False,
                False,
            )
            again = await travel.estimate(
                session, getty, nobu, TravelMode.DRIVING, config
            )
            assert again.from_cache and again.minutes == 24
            assert osrm.call_count == 1

            # Padding is applied as it's read, so a new setting takes effect
            # without a refetch.
            unpadded = await travel.estimate(
                session,
                getty,
                nobu,
                TravelMode.DRIVING,
                settings(TRAVEL_PROVIDER="osrm", OSRM_URL=OSRM, TRAVEL_PADDING_PCT=0),
            )
            assert unpadded.minutes == 20 and osrm.call_count == 1

            # The other direction is its own leg.
            await travel.estimate(session, nobu, getty, TravelMode.DRIVING, config)
            assert osrm.call_count == 2

            row = await repo.cached_travel(
                session, getty.id, nobu.id, TravelMode.DRIVING, "osrm", ttl_days=30
            )
            assert row is not None and row.minutes == 20


async def test_estimate_needs_coordinates(
    db: async_sessionmaker[AsyncSession], seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        bestia = await repo.get_venue(session, seeded["bestia_id"])
        nobu = await repo.get_venue(session, seeded["nobu_id"])
        with pytest.raises(MissingCoordinates, match="Bestia"):
            await travel.estimate(session, nobu, bestia, TravelMode.DRIVING, settings())


async def test_the_same_venue_is_no_trip(
    db: async_sessionmaker[AsyncSession], seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        nobu = await repo.get_venue(session, seeded["nobu_id"])
        leg = await travel.estimate(session, nobu, nobu, TravelMode.WALKING, settings())
        assert (leg.minutes, leg.meters) == (0, 0)


def test_haversine_getty_to_nobu_is_plausible() -> None:
    # Roughly 10 km along PCH; the straight line is ~9.6 km.
    straight = travel.great_circle_m(GETTY, NOBU)
    assert 9_000 < straight < 10_500
    assert math.ceil(straight * 1.4 / 1000 / 40 * 60 * 1.2) == 25
