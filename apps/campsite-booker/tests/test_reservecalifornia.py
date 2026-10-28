"""ReserveCalifornia, and both sites behind `Campgrounds`.

The `rc_*` fixtures aren't saved responses like recreation.gov's: they're
made up in the shape ReserveCalifornia's own script reads (its park and
facility lists, and `search/grid`'s units and slices), with made-up ids.
"""

import json
from datetime import date

import httpx
import pytest
import respx
from campsite_booker import reservecalifornia
from campsite_booker.availability import StayCriteria, find_stays
from campsite_booker.campgrounds import Campgrounds
from campsite_booker.recgov import (
    AVAILABLE,
    Campground,
    RecGovError,
    Stay,
    campground_url,
    is_state_park,
)
from campsite_booker.reservecalifornia import (
    NOT_RESERVABLE,
    RESERVED,
    WALK_IN,
    ReserveCalifornia,
    ReserveCaliforniaError,
    facility_url,
    near_catalog,
    parse_catalog,
    parse_grid,
    search_catalog,
    site_type,
    split_id,
    windows,
)
from conftest import fixture

PFEIFFER = "rc-1001-2001"
NOV6, NOV7, NOV8 = date(2026, 11, 6), date(2026, 11, 7), date(2026, 11, 8)
API = reservecalifornia.API_URL


def catalog():
    return parse_catalog(fixture("rc_places.json"), fixture("rc_facilities.json"))


def grid_sites():
    return {s.campsite_id: s for s in parse_grid(fixture("rc_grid_2001_2026-11.json"))}


def test_the_catalog_keeps_bookable_campgrounds_named_with_their_park():
    found = {listing.campground.id: listing.campground for listing in catalog()}
    # Day use parking and the staff-only camp aren't campgrounds to book.
    assert set(found) == {PFEIFFER, "rc-1002-2003", "rc-1003-2004"}
    pfeiffer = found[PFEIFFER]
    assert pfeiffer.name == "Pfeiffer Big Sur Campground"
    assert (pfeiffer.park, pfeiffer.state) == ("Pfeiffer Big Sur SP", "CA")
    # A facility just called "Campground" says which park.
    assert found["rc-1002-2003"].name == "Limekiln SP: Campground"
    assert pfeiffer.url == "https://www.reservecalifornia.com/park/1001/2001"


def test_big_sur_finds_the_park_by_name_then_the_coast_by_description():
    found = search_catalog(catalog(), "Big Sur")
    assert [c.id for c in found] == [PFEIFFER, "rc-1002-2003"]
    assert search_catalog(catalog(), "doheny")[0].id == "rc-1003-2004"
    assert search_catalog(catalog(), "yosemite") == []


def test_near_finds_state_parks_by_distance():
    found = near_catalog(catalog(), 36.27, -121.81, 40)
    assert [c.id for c in found] == [PFEIFFER, "rc-1002-2003"]
    assert found[0].distance_miles < 3
    assert found[1].distance_miles > found[0].distance_miles


def test_a_night_is_available_only_when_it_can_be_booked_online():
    sites = grid_sites()
    assert sites["50001"].nights == {NOV6: AVAILABLE, NOV7: AVAILABLE, NOV8: AVAILABLE}
    assert sites["50001"].min_nights == {NOV6: 2, NOV7: 2}
    # Nov 7 is locked: in someone's cart.
    assert sites["50002"].nights[NOV7] == RESERVED
    assert set(sites["50003"].nights.values()) == {WALK_IN}
    assert set(sites["50005"].nights.values()) == {NOT_RESERVABLE}
    assert set(sites["50006"].nights.values()) == {RESERVED}
    assert (sites["50002"].site, sites["50002"].campsite_type) == (
        "002",
        "TENT CAMPSITE",
    )
    assert sites["50004"].is_group_site
    assert sites["50001"].max_people == 8


def test_stays_respect_the_minimum_nights():
    sites = grid_sites().values()
    one_night = find_stays(PFEIFFER, sites, StayCriteria(NOV6, NOV8, 1))
    # Site 001 needs 2 nights arriving Nov 6 or 7; group sites are off.
    assert [(s.site, s.check_in) for s in one_night] == [
        ("002", NOV6),
        ("001", NOV8),
        ("002", NOV8),
    ]
    two_nights = find_stays(PFEIFFER, sites, StayCriteria(NOV6, NOV6, 2))
    assert [s.site for s in two_nights] == ["001"]
    assert two_nights[0].url == "https://www.reservecalifornia.com/park/1001/2001"
    assert not two_nights[0].holdable


def test_site_type_drops_the_number():
    assert site_type("Tent Campsite #023") == "TENT CAMPSITE"
    assert site_type("RV Site 12") == "RV SITE"
    assert site_type("Hike/Bike") == "HIKE/BIKE"
    assert site_type("") == "CAMPSITE"


def test_ids_and_links():
    assert is_state_park(PFEIFFER) and not is_state_park("232447")
    assert split_id(PFEIFFER) == ("1001", "2001")
    assert campground_url(PFEIFFER) == facility_url(PFEIFFER)
    assert campground_url("232447").endswith("/camping/campgrounds/232447")
    with pytest.raises(ReserveCaliforniaError, match="rc-park-facility"):
        split_id("rc-2001")
    # Caught wherever recreation.gov's errors are.
    assert issubclass(ReserveCaliforniaError, RecGovError)


def test_windows_cover_the_nights_in_spans():
    assert windows(date(2026, 11, 1), date(2026, 12, 5), 30) == [
        (date(2026, 11, 1), date(2026, 11, 30)),
        (date(2026, 12, 1), date(2026, 12, 5)),
    ]
    assert windows(NOV6, NOV6) == [(NOV6, NOV6)]


@respx.mock
def test_availability_asks_the_grid_per_span_and_merges_the_nights():
    route = respx.post(f"{API}search/grid").mock(
        side_effect=[
            httpx.Response(200, json=fixture("rc_grid_2001_2026-11.json")),
            httpx.Response(
                200,
                json={
                    "Facility": {
                        "Units": {
                            "50001": {
                                "UnitId": 50001,
                                "Name": "Campsite #001",
                                "Slices": {
                                    "2026-12-07T00:00:00": {
                                        "Date": "2026-12-07",
                                        "IsFree": False,
                                    }
                                },
                            }
                        }
                    }
                },
            ),
        ]
    )
    with ReserveCalifornia() as client:
        sites = client.availability(PFEIFFER, NOV6, date(2026, 12, 7))
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert [(b["FacilityId"], b["StartDate"], b["EndDate"]) for b in bodies] == [
        ("2001", "11-06-2026", "12-05-2026"),
        ("2001", "12-06-2026", "12-07-2026"),
    ]
    assert sites["50001"].nights[NOV6] == AVAILABLE
    assert sites["50001"].nights[date(2026, 12, 7)] == RESERVED
    assert sites["50001"].min_nights[NOV6] == 2


@respx.mock
def test_a_grid_without_a_facility_is_an_error():
    respx.post(f"{API}search/grid").mock(
        return_value=httpx.Response(200, json={"Message": "Facility is closed"})
    )
    with ReserveCalifornia() as client, pytest.raises(RecGovError, match="closed"):
        client.availability(PFEIFFER, NOV6, NOV6)


@respx.mock
def test_the_catalog_is_fetched_once_for_searches_and_lookups():
    places = respx.get(f"{API}fd/places").mock(
        return_value=httpx.Response(200, json=fixture("rc_places.json"))
    )
    respx.get(f"{API}fd/facilities").mock(
        return_value=httpx.Response(200, json=fixture("rc_facilities.json"))
    )
    with ReserveCalifornia() as client:
        assert client.search("big sur")[0].id == PFEIFFER
        assert client.campground(PFEIFFER).name == "Pfeiffer Big Sur Campground"
        with pytest.raises(ReserveCaliforniaError, match="No campground"):
            client.campground("rc-1001-9999")
    assert places.call_count == 1


class FakeSite:
    def __init__(self, found=(), fail=False) -> None:
        self.found = list(found)
        self.fail = fail
        self.asked: list[str] = []

    def _answer(self, what):
        self.asked.append(what)
        if self.fail:
            raise RecGovError("down")
        return self.found

    def search(self, query, limit=20):
        return self._answer(f"search {query}")

    def search_near(self, lat, lng, radius, limit=30):
        return self._answer("near")

    def campground(self, campground_id):
        self.asked.append(f"campground {campground_id}")
        return Campground(id=campground_id, name=campground_id)

    def availability(self, campground_id, first, last):
        self.asked.append(f"availability {campground_id}")
        return {}

    def releases(self, campground_id):
        self.asked.append(f"releases {campground_id}")
        return {"current_release": {}}

    def close(self):
        pass


def test_campgrounds_sends_each_id_to_its_site():
    recgov, state = FakeSite(), FakeSite()
    both = Campgrounds(recgov, state)
    both.campground(PFEIFFER)
    both.availability(PFEIFFER, NOV6, NOV7)
    both.campground("232447")
    assert state.asked == [f"campground {PFEIFFER}", f"availability {PFEIFFER}"]
    assert recgov.asked == ["campground 232447"]
    # No release schedule for a state park, and recreation.gov isn't asked.
    assert both.releases(PFEIFFER) == {}
    assert recgov.asked == ["campground 232447"]
    assert both.releases("232447") == {"current_release": {}}


def test_campgrounds_searches_both_and_survives_one_failing():
    kirk = Campground(id="233116", name="Kirk Creek", distance_miles=20)
    pfeiffer = Campground(id=PFEIFFER, name="Pfeiffer", distance_miles=2)
    both = Campgrounds(FakeSite([kirk]), FakeSite([pfeiffer]))
    assert [c.id for c in both.search("big sur")] == [PFEIFFER, "233116"]
    assert [c.id for c in both.search_near(36.2, -121.8, 50)] == [PFEIFFER, "233116"]

    one_down = Campgrounds(FakeSite([kirk]), FakeSite(fail=True))
    assert [c.id for c in one_down.search("big sur")] == ["233116"]
    with pytest.raises(RecGovError):
        Campgrounds(FakeSite(fail=True), FakeSite(fail=True)).search("big sur")


def test_a_state_park_stay_isnt_holdable():
    stay = Stay(PFEIFFER, "50001", "001", "", "CAMPSITE", 8, NOV6, 2)
    assert not stay.holdable
    assert Stay("232447", "100", "044", "", "STANDARD", 6, NOV6, 2).holdable
