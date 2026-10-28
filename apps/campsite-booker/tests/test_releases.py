from datetime import UTC, date, datetime

from campsite_booker.availability import StayCriteria, find_stays
from campsite_booker.releases import Release, parse_releases, released, waiting_on
from conftest import fixture, site

NOW = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)
# Upper Pines: open through Mar 14 2027; Nov 15 at 7 AM Pacific opens
# arrivals through Apr 14.
PINES = parse_releases(fixture("releases_232447.json"), NOW)


def test_parse_a_monthly_release():
    assert PINES.open_through == date(2027, 3, 14)
    assert PINES.next_at == datetime(2026, 11, 15, 15, 0, tzinfo=UTC)
    assert PINES.next_open_through == date(2027, 4, 14)
    assert not PINES.opens(date(2027, 3, 14))
    assert PINES.opens(date(2027, 3, 15))
    assert PINES.opens(date(2027, 4, 14))
    assert not PINES.opens(date(2027, 4, 15))


def test_a_closed_campground_has_no_next_release():
    # Lower Pines: its "next" release a year out opens nothing new.
    closed = parse_releases(fixture("releases_232450_closed.json"), NOW)
    assert closed.next_at is None
    assert not closed.opens(date(2027, 6, 1))


def test_a_release_already_past_is_none():
    later = NOW.replace(year=2027)
    assert parse_releases(fixture("releases_232447.json"), later).next_at is None
    assert parse_releases({}, NOW) == Release(date.min, None, None)


def test_waiting_on_picks_the_dates_the_release_opens():
    crit = StayCriteria(date(2027, 3, 10), date(2027, 3, 20), 2)
    days = waiting_on(crit, PINES, date(2026, 10, 27))
    assert days[0] == date(2027, 3, 15) and days[-1] == date(2027, 3, 20)
    soon = StayCriteria(date(2027, 3, 1), date(2027, 3, 14), 2)
    assert waiting_on(soon, PINES, date(2026, 10, 27)) == []


def statuses(*names: str):
    """Sites whose Mar 15 2027 night has each of these statuses."""
    out = {}
    for i, name in enumerate(names):
        s = site(str(i), [])
        s.nights = {date(2027, 3, 15): name}
        out[s.campsite_id] = s
    return out


def test_released_once_no_site_is_open_or_nyr_on_the_first_new_day():
    # Before: seasonal sites are Closed and stay-overs Reserved already.
    assert not released(statuses("Open", "Closed", "Reserved"), PINES)
    assert not released(statuses("NYR", "Closed"), PINES)
    assert released(statuses("Available", "Closed", "Reserved"), PINES)
    assert released(statuses("Reserved", "Closed"), PINES)
    assert not released({}, PINES)


def test_released_when_only_a_later_month_was_fetched():
    # A watch arriving in April fetches April only; Mar 15 isn't there.
    s = site("1", [])
    s.nights = {date(2027, 4, 5): "NYR"}
    assert not released({"1": s}, PINES)
    s.nights = {date(2027, 4, 5): "Available"}
    assert released({"1": s}, PINES)


def test_a_stay_may_end_in_open_nights_but_not_start_in_one():
    s = site("1", [])
    s.nights = {
        date(2027, 3, 13): "Available",
        date(2027, 3, 14): "Available",
        date(2027, 3, 15): "Open",
        date(2027, 3, 16): "Open",
    }
    crit = StayCriteria(date(2027, 3, 13), date(2027, 3, 15), 2)
    found = [st.check_in for st in find_stays("x", [s], crit)]
    assert found == [date(2027, 3, 13), date(2027, 3, 14)]
