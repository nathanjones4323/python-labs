from datetime import date

import httpx
import pytest
import respx
from campsite_booker import recgov
from campsite_booker.recgov import (
    RecGov,
    RecGovError,
    months_between,
    parse_month,
    parse_search,
)
from conftest import fixture, scorpion_november


def test_parse_month_reads_each_site_and_its_nights():
    sites = {s.campsite_id: s for s in scorpion_november()}
    assert set(sites) == {"4924", "4925", "4926", "4939"}
    site = sites["4924"]
    assert (site.site, site.loop, site.max_people) == ("022", "Lower Loop", 6)
    assert site.campsite_type == "TENT ONLY NONELECTRIC"
    # Keys are local dates written as midnight UTC; the date is what counts.
    assert site.nights[date(2026, 11, 1)] == "Available"
    assert site.nights[date(2026, 11, 3)] == "Reserved"
    assert len(site.nights) == 21
    assert sites["4939"].is_group_site
    assert not site.is_group_site


def test_parse_search_keeps_campgrounds_with_title_case_names():
    found = parse_search(fixture("search_geo.json"))
    assert [c.id for c in found] == ["232138", "232342", "273845"]
    wheeler = found[0]
    assert wheeler.name == "Wheeler Gorge"
    assert wheeler.park == "Los Padres National Forest"
    assert wheeler.distance_miles == pytest.approx(27.2)
    assert (wheeler.price_min, wheeler.price_max) == (33.97, 70)
    assert wheeler.url.endswith("/camping/campgrounds/232138")


def test_parse_search_skips_other_entities():
    payload = {
        "results": [
            {"entity_type": "recarea", "entity_id": "1", "name": "A Park"},
            {"entity_type": "campground", "entity_id": "2", "name": "Mixed Case"},
        ]
    }
    assert [c.name for c in parse_search(payload)] == ["Mixed Case"]


def test_months_between_crosses_a_year():
    assert months_between(date(2026, 12, 20), date(2027, 2, 1)) == [
        date(2026, 12, 1),
        date(2027, 1, 1),
        date(2027, 2, 1),
    ]
    assert months_between(date(2026, 11, 5), date(2026, 11, 6)) == [date(2026, 11, 1)]


def _month(campsite_id: str, nights: dict[str, str]) -> dict:
    return {
        "campsites": {
            campsite_id: {
                "campsite_id": campsite_id,
                "site": "001",
                "loop": "A",
                "campsite_type": "STANDARD NONELECTRIC",
                "type_of_use": "Overnight",
                "max_num_people": 6,
                "availabilities": {f"{d}T00:00:00Z": v for d, v in nights.items()},
            }
        }
    }


@respx.mock
def test_availability_fetches_each_month_and_merges_them(monkeypatch):
    route = respx.get(
        "https://www.recreation.gov/api/camps/availability/campground/1/month"
    ).mock(
        side_effect=[
            httpx.Response(200, json=_month("9", {"2026-11-30": "Available"})),
            httpx.Response(200, json=_month("9", {"2026-12-01": "Reserved"})),
        ]
    )
    with RecGov() as client:
        sites = client.availability("1", date(2026, 11, 30), date(2026, 12, 1))
    assert [c.request.url.params["start_date"] for c in route.calls] == [
        "2026-11-01T00:00:00.000Z",
        "2026-12-01T00:00:00.000Z",
    ]
    assert sites["9"].nights == {
        date(2026, 11, 30): "Available",
        date(2026, 12, 1): "Reserved",
    }


@respx.mock
def test_retries_a_rate_limit_then_gives_up_on_a_404(monkeypatch):
    monkeypatch.setattr(recgov.time, "sleep", lambda s: None)
    url = "https://www.recreation.gov/api/camps/campgrounds/7"
    respx.get(url).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json={"campground": {"facility_name": "UPPER PINES"}}),
        ]
    )
    respx.get("https://www.recreation.gov/api/search").mock(
        return_value=httpx.Response(500)
    )
    with RecGov() as client:
        # Search failing leaves just the park unknown.
        assert client.campground("7").name == "Upper Pines"

    respx.get("https://www.recreation.gov/api/camps/campgrounds/8").mock(
        return_value=httpx.Response(404)
    )
    with RecGov() as client, pytest.raises(RecGovError, match="404"):
        client.campground("8")


@respx.mock
def test_a_campground_without_a_town_is_known_by_its_park():
    respx.get("https://www.recreation.gov/api/camps/campgrounds/231958").mock(
        return_value=httpx.Response(
            200,
            json={
                "campground": {"facility_id": "231958", "facility_name": "ARROYO SECO"}
            },
        )
    )
    search = respx.get("https://www.recreation.gov/api/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "entity_type": "campground",
                        "entity_id": "231958",
                        "name": "ARROYO SECO",
                        "parent_name": "LOS PADRES NATIONAL FOREST",
                    }
                ]
            },
        )
    )
    with RecGov() as client:
        arroyo = client.campground("231958")
    assert search.calls[0].request.url.params["fq"] == "entity_id:231958"
    assert arroyo.park == "Los Padres National Forest"
    assert arroyo.area == "Los Padres NF"


def test_parse_month_round_trip_with_month_fixture_shape():
    assert parse_month({"campsites": {}}) == []
