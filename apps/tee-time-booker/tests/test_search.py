"""Searching many courses at once, with the browser and the sites faked out."""

import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, time

import pytest
from tee_time_booker import runner
from tee_time_booker.config import Settings
from tee_time_booker.courses import OLIVAS_LINKS, SATICOY, SOULE_PARK, STERLING_HILLS
from tee_time_booker.models import SearchCriteria, TeeTime
from tee_time_booker.runner import CourseSearch, search_courses

CRITERIA = SearchCriteria(
    target_date=date(2026, 11, 1),
    earliest=time(7),
    latest=time(10),
    players=2,
    holes=18,
)


class FakeSites:
    """Stands in for the browser and the adapters: records each browser
    opened and each sign-in, and searches return one slot per course."""

    def __init__(self, fail_sign_in=frozenset(), fail_search=frozenset(), meet=None):
        self.fail_sign_in = fail_sign_in
        self.fail_search = fail_search
        # A barrier every browser waits at: passes only if they're open at once.
        self.meet = meet
        self.browsers = 0
        self.sign_ins = []

    def install(self, monkeypatch):
        sites = self

        @contextmanager
        def browser_session(**kwargs):
            sites.browsers += 1
            if sites.meet is not None:
                sites.meet.wait()
            yield object()

        class Platform:
            def __init__(self, course):
                self.course = course

            def start(self, criteria):
                sites.sign_ins.append(self.course.key)
                if self.course.platform in sites.fail_sign_in:
                    raise RuntimeError("Login failed (403)")

            def find_tee_times(self, criteria):
                if self.course.key in sites.fail_search:
                    raise RuntimeError("search returned 500")
                start = datetime.combine(criteria.target_date, time(8))
                return [TeeTime(self.course.platform, self.course.name, start)]

        monkeypatch.setattr(runner, "browser_session", browser_session)
        monkeypatch.setattr(runner, "load_credentials", lambda *a, **k: None)
        monkeypatch.setattr(
            runner, "build_platform", lambda course, creds, session: Platform(course)
        )


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=None,
        plan_id=None,
        submit_payment=False,
        headless=True,
        artifacts_dir=tmp_path,
        target_date_override=None,
    )


ALL = [
    CourseSearch(SOULE_PARK, "SOULE"),
    CourseSearch(SATICOY, "GOLFNOW"),
    CourseSearch(OLIVAS_LINKS, "GOLFNOW"),
    CourseSearch(STERLING_HILLS, "GOLFNOW"),
]


def test_one_sign_in_per_site_and_the_sites_in_parallel(monkeypatch, settings):
    # Both sites' browsers must be open together to get past the barrier.
    sites = FakeSites(meet=threading.Barrier(2, timeout=5))
    sites.install(monkeypatch)

    results = search_courses(ALL, CRITERIA, settings)

    assert set(results.tee_times) == {c.course.key for c in ALL}
    assert results.errors == {}
    # GolfNow signs in once for its three courses; one browser per site.
    assert sorted(sites.sign_ins) == ["saticoy", "soule-park"]
    assert sites.browsers == 2


def test_a_site_signing_in_per_course_signs_in_to_each(monkeypatch, settings):
    sites = FakeSites()
    sites.install(monkeypatch)
    other = replace(SOULE_PARK, key="other-foreup")
    search_courses(
        [CourseSearch(SOULE_PARK, "SOULE"), CourseSearch(other, "SOULE")],
        CRITERIA,
        settings,
    )
    assert sites.sign_ins == ["soule-park", "other-foreup"]


def test_a_failed_sign_in_fails_that_sites_courses_only(monkeypatch, settings):
    sites = FakeSites(fail_sign_in={"golfnow"})
    sites.install(monkeypatch)

    results = search_courses(ALL, CRITERIA, settings)

    assert set(results.tee_times) == {"soule-park"}
    assert results.errors == {
        key: "RuntimeError: Login failed (403)"
        for key in ("saticoy", "olivas-links", "sterling-hills")
    }
    # Not tried again for every course.
    assert sites.sign_ins.count("saticoy") == 1


def test_one_course_failing_leaves_the_rest_of_its_site(monkeypatch, settings):
    sites = FakeSites(fail_search={"olivas-links"})
    sites.install(monkeypatch)
    results = search_courses(ALL, CRITERIA, settings)
    assert results.errors == {"olivas-links": "RuntimeError: search returned 500"}
    assert "sterling-hills" in results.tee_times


def test_missing_credentials_fail_the_site_without_a_browser(monkeypatch, settings):
    sites = FakeSites()
    sites.install(monkeypatch)

    def load_credentials(course, prefix, *, with_payment):
        if prefix == "GOLFNOW":
            raise RuntimeError("Missing required environment variable GOLFNOW_EMAIL")

    monkeypatch.setattr(runner, "load_credentials", load_credentials)
    results = search_courses(ALL, CRITERIA, settings)
    assert set(results.tee_times) == {"soule-park"}
    assert len(results.errors) == 3 and sites.browsers == 1


def test_nothing_to_search_is_nothing_found(settings):
    assert search_courses([], CRITERIA, settings) == runner.SearchResults()
