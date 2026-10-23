"""Looking for tee times across courses, with no plan behind the search."""

from datetime import date, time

from fastapi import APIRouter, Query

from tee_time_booker.api.deps import AppSettings, Sessions
from tee_time_booker.api.schemas import FoundTeeTimeOut, TeeTimeOut, TeeTimeSearchOut
from tee_time_booker.courses import (
    DEFAULT_SEARCH_MILES,
    MAX_SEARCH_MILES,
    miles_from_home,
)
from tee_time_booker.db import Course
from tee_time_booker.db.service import (
    NotFound,
    course_config,
    get_course,
    get_user,
    list_courses,
    site_account_for,
)
from tee_time_booker.models import SearchCriteria
from tee_time_booker.runner import CourseSearch, search_courses

router = APIRouter(prefix="/tee-times", tags=["tee times"])


def within_range(course: Course, miles: float) -> bool:
    """Whether `course` is within `miles` of home; unknown locations count."""
    distance = miles_from_home(course.latitude, course.longitude)
    return distance is None or distance <= miles


@router.get(
    "",
    operation_id="search_tee_times",
    summary="What's open on a day across courses, without booking anything",
    description=(
        "Searches the courses within `within_miles` of Ventura, CA (or just "
        "`courses`, wherever they are) for slots on `date` in the tee-off "
        "window that seat the group, with the user's account on each site. "
        f"The default radius ({DEFAULT_SEARCH_MILES:g}) covers Ventura County, "
        "Ojai, the Conejo Valley and Santa Barbara; raise it (up to "
        f"{MAX_SEARCH_MILES:g} reaches Los Angeles, Pasadena, Long Beach and "
        "Solvang) only when the user asks to look farther out. One "
        "sign-in per site, the sites in parallel: about a minute whether it's "
        "one course or all of them. A course the user has no account for is "
        "listed in not_searched."
    ),
)
def search(
    sessions: Sessions,
    settings: AppSettings,
    user_email: str,
    date_: date = Query(alias="date"),
    players: int = Query(ge=1, le=4),
    holes: int = Query(ge=1),
    earliest: time = Query(
        default=time.min, description="Earliest tee-off, course-local"
    ),
    latest: time = Query(default=time.max, description="Latest tee-off, course-local"),
    courses: list[str] | None = Query(
        default=None,
        description="Course keys to search; default: every course in range",
    ),
    within_miles: float = Query(
        default=DEFAULT_SEARCH_MILES,
        gt=0,
        le=MAX_SEARCH_MILES,
        description=(
            "Search the courses this many straight-line miles from Ventura, CA "
            "(see list_courses' miles_from_home); ignored when `courses` is given"
        ),
    ),
) -> TeeTimeSearchOut:
    searches: list[CourseSearch] = []
    not_searched: dict[str, str] = {}
    names: dict[str, str] = {}
    with sessions() as session:
        user = get_user(session, user_email)
        rows = (
            [get_course(session, key) for key in dict.fromkeys(courses)]
            if courses
            else [
                row for row in list_courses(session) if within_range(row, within_miles)
            ]
        )
        for row in rows:
            names[row.key] = row.name
            try:
                account = site_account_for(session, user, row)
            except (NotFound, ValueError) as e:
                not_searched[row.key] = str(e)
                continue
            searches.append(CourseSearch(course_config(row), account.env_prefix))
    criteria = SearchCriteria(
        target_date=date_,
        earliest=earliest,
        latest=latest,
        players=players,
        holes=holes,
    )
    results = search_courses(searches, criteria, settings)
    found = [
        FoundTeeTimeOut(
            **TeeTimeOut.from_tee_time(t).model_dump(),
            course=key,
            course_name=names[key],
        )
        for key, tee_times in results.tee_times.items()
        for t in tee_times
        if earliest <= t.start.time() <= latest
    ]
    return TeeTimeSearchOut(
        tee_times=sorted(found, key=lambda t: (t.start, t.course)),
        searched=sorted(results.tee_times),
        not_searched={**not_searched, **results.errors},
    )
