"""Google Places lookup: name matching, ranking by distance, and the
request and response shapes (mocked)."""

import json

import httpx
import pytest
import respx
from date_planner.booking import places
from date_planner.booking.places import Place
from date_planner.errors import PlacesFailed
from date_planner.matching import names_match
from date_planner.planning.travel import LatLng

BESTIA = LatLng(34.0336, -118.2293)


@pytest.mark.parametrize(
    ("ours", "theirs", "expected"),
    [
        ("Nobu Malibu", "Nobu", True),
        ("A Tí", "A TÍ Restaurant", True),
        ("The Bavel", "Bavel", True),
        ("Catch & Release", "Catch and Release", True),
        ("Bestia", "Bavel", False),
        ("Nobu Malibu", "Nobu Los Angeles", False),
        ("", "Bestia", False),
    ],
)
def test_names_match(ours: str, theirs: str, expected: bool) -> None:
    assert names_match(ours, theirs) is expected


def test_a_namesake_across_town_is_not_a_match() -> None:
    here = Place("near", "Bestia", "2121 E 7th Pl", LatLng(34.0337, -118.2294))
    across = Place("far", "Bestia", "Somewhere else", LatLng(34.1, -118.4))
    other = Place("other", "Bavel", "500 Mateo St", LatLng(34.0336, -118.2293))
    ranked = places.rank("Bestia", BESTIA, [across, other, here])
    assert [c.is_match for c in ranked] == [False, False, True]
    assert ranked[2].distance_m is not None and ranked[2].distance_m < 50


def test_without_coordinates_the_name_decides() -> None:
    ranked = places.rank("Bestia", None, [Place("x", "Bestia", None, None)])
    assert ranked[0].distance_m is None
    assert ranked[0].is_match


async def test_search_sends_the_key_mask_and_bias() -> None:
    body = {
        "places": [
            {
                "id": "ChIJ-bestia",
                "displayName": {"text": "Bestia", "languageCode": "en"},
                "formattedAddress": "2121 E 7th Pl, Los Angeles, CA 90021, USA",
                "location": {"latitude": 34.0337, "longitude": -118.2294},
            },
            {"displayName": {"text": "no id, skipped"}},
        ]
    }
    with respx.mock() as router:
        route = router.post(places.SEARCH_URL).respond(200, json=body)
        found = await places.search("Bestia, Los Angeles", "k3y", BESTIA)
    sent = route.calls.last.request
    assert sent.headers["X-Goog-Api-Key"] == "k3y"
    assert sent.headers["X-Goog-FieldMask"] == places.FIELD_MASK
    payload = json.loads(sent.content)
    assert payload["textQuery"] == "Bestia, Los Angeles"
    assert payload["locationBias"]["circle"]["center"] == {
        "latitude": BESTIA.lat,
        "longitude": BESTIA.lng,
    }
    assert found == [
        Place(
            "ChIJ-bestia",
            "Bestia",
            "2121 E 7th Pl, Los Angeles, CA 90021, USA",
            LatLng(34.0337, -118.2294),
        )
    ]


async def test_no_results_is_an_empty_list() -> None:
    with respx.mock() as router:
        router.post(places.SEARCH_URL).respond(200, json={})
        assert await places.search("Nowhere", "k3y") == []


async def test_googles_reason_is_passed_on() -> None:
    error = {"error": {"code": 403, "message": "Places API (New) has not been used"}}
    with respx.mock() as router:
        router.post(places.SEARCH_URL).respond(403, json=error)
        with pytest.raises(PlacesFailed, match="403: Places API \\(New\\) has not"):
            await places.search("Bestia", "k3y")


async def test_a_network_failure_is_places_failed() -> None:
    with respx.mock() as router:
        router.post(places.SEARCH_URL).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(PlacesFailed, match="request failed"):
            await places.search("Bestia", "k3y")
