"""ReserveCalifornia, and both sites behind `Campgrounds`.

The `rc_*` fixtures are ReserveCalifornia's responses of Oct 28, 2026,
trimmed: the parks and facilities around Big Sur and Doheny, and Pfeiffer
Big Sur's South Camp for Nov 6–11 (four of its sites, and a group site from
Group Sites A & B). The night statuses the site has no example of right now
(walk-in, a drawing, a weekend minimum) are set on a copy of a real site.
"""

import copy
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
    CONFIG_URL,
    DRAWING,
    NOT_RESERVABLE,
    RESERVED,
    WALK_IN,
    ReserveCalifornia,
    ReserveCaliforniaError,
    api_url,
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

SOUTH_CAMP = "rc-690-611"
GROUP_AB = "rc-690-609"
NOV = {d: date(2026, 11, d) for d in range(6, 13)}
# The sites in the grid fixture, by unit id.
SC09, SC13, SC59, SC68, PBGA = "43786", "43729", "43790", "43792", "43670"
API = "https://rdr.example/rdr/"


@pytest.fixture
def config():
    """ReserveCalifornia's `config.json`, naming the API host."""
    with respx.mock:
        respx.get(CONFIG_URL).mock(
            return_value=httpx.Response(200, json={"rdrApiUrl": API})
        )
        yield


def catalog():
    return parse_catalog(fixture("rc_places.json"), fixture("rc_facilities.json"))


def grid():
    return fixture("rc_grid_611_2026-11.json")


def grid_sites(payload=None):
    return {s.campsite_id: s for s in parse_grid(payload or grid())}


def with_unit(payload, unit_id, **slice_changes):
    """`payload` with a copy of a site's Nov 8 changed (and the site renamed
    to `<id>x`), for statuses the live grid had no example of."""
    payload = copy.deepcopy(payload)
    units = payload["Facility"]["Units"]
    source = next(u for u in units.values() if str(u["UnitId"]) == unit_id)
    unit = copy.deepcopy(source)
    unit["UnitId"] = f"{unit_id}x"
    unit.update(slice_changes.pop("unit", {}))
    unit["Slices"]["2026-11-08T00:00:00"].update(slice_changes)
    units["new"] = unit
    return payload


def test_the_catalog_keeps_campgrounds_named_with_their_park():
    found = {listing.campground.id: listing.campground for listing in catalog()}
    # The day use lots aren't campgrounds.
    assert "rc-690-2092" not in found and "rc-690-2093" not in found
    south = found[SOUTH_CAMP]
    assert south.name == "Pfeiffer Big Sur SP: South Camp (sites 1-78)"
    assert (south.park, south.state) == ("Pfeiffer Big Sur SP", "CA")
    # Facilities have no location of their own: the park's.
    assert (south.latitude, south.longitude) == (36.25305556, -121.7811111)
    assert south.reservable
    # A name that already says its park isn't prefixed.
    assert found["rc-639-581"].name == "Doheny Group"
    # Group Sites A & B book by phone only.
    assert not found[GROUP_AB].reservable
    assert south.url == "https://www.reservecalifornia.com/park/690/611"


def test_search_matches_every_word_of_the_park_or_facility_name():
    assert [c.id for c in search_catalog(catalog(), "Big Sur")] == [
        GROUP_AB,
        SOUTH_CAMP,
        "rc-690-612",
        "rc-690-767",
    ]
    assert [c.id for c in search_catalog(catalog(), "doheny south")] == ["rc-639-464"]
    assert search_catalog(catalog(), "yosemite") == []


def test_near_finds_the_big_sur_coast_by_distance():
    found = near_catalog(catalog(), 36.25, -121.78, 25)
    parks = [c.park for c in found]
    # Pfeiffer Big Sur's four, Andrew Molera, Julia Pfeiffer Burns, then
    # Limekiln's two camps; not Doheny.
    assert parks[:4] == ["Pfeiffer Big Sur SP"] * 4
    assert parks[4:] == [
        "Andrew Molera SP",
        "Julia Pfeiffer Burns SP",
        "Limekiln SP",
        "Limekiln SP",
    ]
    assert [c.distance_miles for c in found] == sorted(c.distance_miles for c in found)


def test_a_night_is_available_only_when_free_and_unlocked():
    sites = grid_sites()
    assert sites[SC09].nights == {
        NOV[6]: RESERVED,
        NOV[7]: RESERVED,
        NOV[8]: AVAILABLE,
        NOV[9]: AVAILABLE,
        NOV[10]: AVAILABLE,
        NOV[11]: RESERVED,
    }
    # Nov 9–11 are locked: in someone's cart.
    assert sites[SC13].nights[NOV[8]] == AVAILABLE
    assert sites[SC13].nights[NOV[9]] == RESERVED
    assert set(sites[SC59].nights.values()) == {RESERVED}
    assert sites[SC68].nights[NOV[7]] == RESERVED
    assert sites[SC68].nights[NOV[9]] == AVAILABLE
    assert sites[SC09].min_nights == {}
    # ReserveCalifornia doesn't say how many a site holds.
    assert sites[SC09].max_people == 0


def test_nights_the_website_wont_sell():
    payload = with_unit(grid(), SC09, IsWalkin=True)
    assert grid_sites(payload)[f"{SC09}x"].nights[NOV[8]] == WALK_IN
    payload = with_unit(grid(), SC09, IsReservationDraw=True)
    assert grid_sites(payload)[f"{SC09}x"].nights[NOV[8]] == DRAWING
    payload = with_unit(grid(), SC09, unit={"AllowWebBooking": False})
    assert set(grid_sites(payload)[f"{SC09}x"].nights.values()) == {NOT_RESERVABLE}


def test_sites_are_named_and_typed_from_the_unit_name():
    sites = grid_sites()
    assert (sites[SC09].site, sites[SC09].campsite_type) == ("SC09", "TENT CAMPSITE")
    assert sites[SC13].campsite_type == "CAMPSITE"
    assert sites[SC59].campsite_type == "PREMIUM TENT CAMPSITE"
    assert sites[PBGA].campsite_type == "GROUP HIKE IN CAMPSITE"
    assert sites[PBGA].is_group_site and not sites[SC09].is_group_site


def test_site_type_drops_the_number_whatever_its_letters():
    # Shapes from across the state parks, Oct 2026.
    assert site_type("Tent Campsite #023") == "TENT CAMPSITE"
    assert site_type("Tent Campsite #SC01") == "TENT CAMPSITE"
    assert site_type("Boat In Primitive Campsite #B103") == "BOAT IN PRIMITIVE CAMPSITE"
    assert site_type("Cabin (6 People) #CA12") == "CABIN (6 PEOPLE)"
    assert site_type("Group Campsite #GCAB") == "GROUP CAMPSITE"
    assert site_type("Hook Up (E/W) Campsite #7") == "HOOK UP (E/W) CAMPSITE"
    assert site_type("RV Site 12") == "RV SITE"
    assert site_type("Hike/Bike") == "HIKE/BIKE"
    assert site_type("") == "CAMPSITE"


def test_stays_need_every_night_free():
    sites = grid_sites().values()
    two_nights = find_stays(SOUTH_CAMP, sites, StayCriteria(NOV[8], NOV[9], 2))
    assert [(s.site, s.check_in) for s in two_nights] == [
        ("SC09", NOV[8]),
        ("SC09", NOV[9]),
        ("SC68", NOV[9]),
    ]
    assert two_nights[0].url == "https://www.reservecalifornia.com/park/690/611"
    assert not two_nights[0].holdable
    tents = find_stays(
        SOUTH_CAMP, sites, StayCriteria(NOV[8], NOV[8], 1, site_types=("tent",))
    )
    assert [s.site for s in tents] == ["SC09"]


def test_stays_respect_a_minimum_stay():
    payload = with_unit(grid(), SC09, MinStay=3)
    sites = grid_sites(payload)
    assert sites[f"{SC09}x"].min_nights == {NOV[8]: 3}
    found = find_stays(SOUTH_CAMP, [sites[f"{SC09}x"]], StayCriteria(NOV[8], NOV[9], 2))
    # Arriving Nov 8 needs 3 nights; Nov 9 is fine.
    assert [s.check_in for s in found] == [NOV[9]]


def test_ids_and_links():
    assert is_state_park(SOUTH_CAMP) and not is_state_park("232447")
    assert split_id(SOUTH_CAMP) == ("690", "611")
    assert campground_url(SOUTH_CAMP) == facility_url(SOUTH_CAMP)
    assert campground_url("232447").endswith("/camping/campgrounds/232447")
    with pytest.raises(ReserveCaliforniaError, match="rc-park-facility"):
        split_id("rc-611")
    # Caught wherever recreation.gov's errors are.
    assert issubclass(ReserveCaliforniaError, RecGovError)


def test_windows_cover_the_nights_in_spans():
    assert windows(date(2026, 11, 1), date(2026, 12, 5), 30) == [
        (date(2026, 11, 1), date(2026, 11, 30)),
        (date(2026, 12, 1), date(2026, 12, 5)),
    ]
    assert windows(NOV[6], NOV[6]) == [(NOV[6], NOV[6])]


@respx.mock
def test_the_api_host_comes_from_the_sites_config():
    respx.get(CONFIG_URL).mock(
        return_value=httpx.Response(200, json={"rdrApiUrl": "https://rdr.example/rdr"})
    )
    assert api_url() == "https://rdr.example/rdr/"


@respx.mock
def test_the_known_api_host_when_the_config_cant_be_read():
    respx.get(CONFIG_URL).mock(return_value=httpx.Response(503))
    assert api_url() == reservecalifornia.API_URL
    respx.get(CONFIG_URL).mock(return_value=httpx.Response(200, json={}))
    assert api_url() == reservecalifornia.API_URL


def test_availability_asks_the_grid_per_span_and_merges_the_nights(config):
    later = {
        "Facility": {
            "Units": {
                "1.1": {
                    "UnitId": int(SC09),
                    "Name": "Tent Campsite #SC09",
                    "ShortName": "SC09",
                    "Slices": {
                        "2026-12-07T00:00:00": {"Date": "2026-12-07", "IsFree": False}
                    },
                }
            }
        }
    }
    route = respx.post(f"{API}search/grid").mock(
        side_effect=[
            httpx.Response(200, json=grid()),
            httpx.Response(200, json=later),
        ]
    )
    with ReserveCalifornia() as client:
        sites = client.availability(SOUTH_CAMP, NOV[6], date(2026, 12, 7))
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert [(b["FacilityId"], b["StartDate"], b["EndDate"]) for b in bodies] == [
        ("611", "2026-11-06", "2026-12-05"),
        ("611", "2026-12-06", "2026-12-07"),
    ]
    assert sites[SC09].nights[NOV[8]] == AVAILABLE
    assert sites[SC09].nights[date(2026, 12, 7)] == RESERVED


def test_an_unknown_facility_is_an_error(config):
    # What the grid says for a facility id it doesn't have (Oct 2026).
    respx.post(f"{API}search/grid").mock(
        return_value=httpx.Response(
            200,
            json={
                "Message": "Invalid FacilityId specified for facility grid search",
                "Facility": {"FacilityId": 999999, "Name": None, "Units": None},
            },
        )
    )
    with ReserveCalifornia() as client, pytest.raises(RecGovError, match="Invalid"):
        client.availability("rc-690-999999", NOV[6], NOV[6])


def test_the_catalog_is_fetched_once_for_searches_and_lookups(config):
    places = respx.get(f"{API}fd/places").mock(
        return_value=httpx.Response(200, json=fixture("rc_places.json"))
    )
    respx.get(f"{API}fd/facilities").mock(
        return_value=httpx.Response(200, json=fixture("rc_facilities.json"))
    )
    with ReserveCalifornia() as client:
        assert client.search("big sur")[1].id == SOUTH_CAMP
        assert client.campground(SOUTH_CAMP).park == "Pfeiffer Big Sur SP"
        with pytest.raises(ReserveCaliforniaError, match="No campground"):
            client.campground("rc-690-999999")
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
    both.campground(SOUTH_CAMP)
    both.availability(SOUTH_CAMP, NOV[6], NOV[7])
    both.campground("232447")
    assert state.asked == [f"campground {SOUTH_CAMP}", f"availability {SOUTH_CAMP}"]
    assert recgov.asked == ["campground 232447"]
    # No release schedule for a state park, and recreation.gov isn't asked.
    assert both.releases(SOUTH_CAMP) == {}
    assert recgov.asked == ["campground 232447"]
    assert both.releases("232447") == {"current_release": {}}


def test_campgrounds_searches_both_and_survives_one_failing():
    kirk = Campground(id="233116", name="Kirk Creek", distance_miles=20)
    pfeiffer = Campground(id=SOUTH_CAMP, name="Pfeiffer", distance_miles=2)
    both = Campgrounds(FakeSite([kirk]), FakeSite([pfeiffer]))
    assert [c.id for c in both.search("big sur")] == [SOUTH_CAMP, "233116"]
    assert [c.id for c in both.search_near(36.2, -121.8, 50)] == [SOUTH_CAMP, "233116"]

    one_down = Campgrounds(FakeSite([kirk]), FakeSite(fail=True))
    assert [c.id for c in one_down.search("big sur")] == ["233116"]
    with pytest.raises(RecGovError):
        Campgrounds(FakeSite(fail=True), FakeSite(fail=True)).search("big sur")


def test_a_state_park_stay_isnt_holdable():
    stay = Stay(SOUTH_CAMP, SC09, "SC09", "", "CAMPSITE", 8, NOV[6], 2)
    assert not stay.holdable
    assert Stay("232447", "100", "044", "", "STANDARD", 6, NOV[6], 2).holdable
