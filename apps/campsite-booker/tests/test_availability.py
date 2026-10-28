from datetime import date

import pytest
from campsite_booker.availability import StayCriteria, find_stays, parse_weekdays
from conftest import SCORPION, scorpion_november, site


def stays(criteria: StayCriteria, today: date | None = None):
    found = find_stays(SCORPION, scorpion_november(), criteria, today)
    return [(s.site, s.check_in.day) for s in found]


NOV = date(2026, 11, 1)


def test_two_nights_anywhere_in_the_first_three_weeks():
    crit = StayCriteria(NOV, date(2026, 11, 20), nights=2)
    # Earliest arrival first; the group site is left out.
    assert stays(crit) == [
        ("022", 1),
        ("020", 3),
        ("022", 4),
        ("020", 17),
        ("020", 18),
        ("023", 18),
    ]


def test_exact_dates():
    assert stays(StayCriteria(date(2026, 11, 4), date(2026, 11, 4), 2)) == [("022", 4)]
    assert stays(StayCriteria(date(2026, 11, 3), date(2026, 11, 3), 3)) == []


def test_a_night_that_wasnt_fetched_isnt_free():
    # Free on Nov 30; Dec 1 isn't in the data at all.
    sites = [site("1", [date(2026, 11, 30)])]
    one = StayCriteria(date(2026, 11, 30), date(2026, 11, 30), 1)
    two = StayCriteria(date(2026, 11, 30), date(2026, 11, 30), 2)
    assert len(find_stays("x", sites, one)) == 1
    assert find_stays("x", sites, two) == []


def test_group_sites_only_when_asked_and_group_size_filters():
    crit = StayCriteria(NOV, NOV, 1, group_size=10, include_group_sites=True)
    assert stays(crit) == [("A", 1)]
    crit = StayCriteria(NOV, NOV, 1, group_size=7)
    assert stays(crit) == []


def test_weekdays_types_and_sites():
    # Nov 6, 2026 is a Friday; nothing for 2 nights from a Friday or Saturday.
    weekend = StayCriteria(NOV, date(2026, 11, 20), 2, frozenset({4, 5}))
    assert stays(weekend) == []
    tues = StayCriteria(NOV, date(2026, 11, 20), 2, frozenset({1}))
    assert stays(tues) == [("020", 3), ("020", 17)]
    rv = StayCriteria(NOV, date(2026, 11, 20), 2, site_types=("rv",))
    assert stays(rv) == []
    only = StayCriteria(NOV, date(2026, 11, 20), 2, campsite_ids=frozenset({"4926"}))
    assert stays(only) == [("023", 18)]


def test_arrivals_in_the_past_are_skipped():
    crit = StayCriteria(NOV, date(2026, 11, 20), 2)
    assert stays(crit, today=date(2026, 11, 5)) == [
        ("020", 17),
        ("020", 18),
        ("023", 18),
    ]


def test_criteria_guard_against_typos():
    with pytest.raises(ValueError):
        StayCriteria(NOV, NOV, 0)
    with pytest.raises(ValueError):
        StayCriteria(date(2026, 11, 2), NOV, 1)
    with pytest.raises(ValueError):
        StayCriteria(NOV, NOV, 1, frozenset({7}))


def test_parse_weekdays():
    assert parse_weekdays(["Fri", "saturday", 6, "0"]) == {4, 5, 6, 0}
    assert parse_weekdays(None) == frozenset()
    with pytest.raises(ValueError):
        parse_weekdays(["Funday"])
