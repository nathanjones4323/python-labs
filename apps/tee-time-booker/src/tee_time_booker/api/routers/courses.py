from datetime import date, time

from fastapi import APIRouter, Query

from tee_time_booker.api.deps import AppSettings, Sessions
from tee_time_booker.api.schemas import CourseOut, TeeTimeOut
from tee_time_booker.db.service import (
    course_config,
    get_course,
    get_user,
    list_courses,
    site_account_for,
)
from tee_time_booker.models import SearchCriteria
from tee_time_booker.runner import find_tee_times

router = APIRouter(prefix="/courses", tags=["courses"])


@router.get("", operation_id="list_courses", summary="The bookable courses")
def list_(sessions: Sessions) -> list[CourseOut]:
    with sessions() as session:
        return [CourseOut.from_row(c) for c in list_courses(session)]


@router.get("/{key}", operation_id="get_course", summary="One course by key")
def get(key: str, sessions: Sessions) -> CourseOut:
    with sessions() as session:
        return CourseOut.from_row(get_course(session, key))


@router.get(
    "/{key}/tee-times",
    operation_id="find_course_tee_times",
    summary="What's bookable at a course on a day, without booking anything",
    description=(
        "Signs in to the course's site with the user's account there and lists "
        "every slot on `date` that seats the group, then closes the browser. "
        "No plan needed. Takes up to a minute or two."
    ),
)
def tee_times(
    key: str,
    sessions: Sessions,
    settings: AppSettings,
    user_email: str,
    date_: date = Query(alias="date"),
    players: int = Query(ge=1, le=4),
    holes: int = Query(ge=1),
    env_prefix: str | None = Query(
        default=None,
        description="Which of the user's accounts signs in; default: their only one",
    ),
) -> list[TeeTimeOut]:
    with sessions() as session:
        user = get_user(session, user_email)
        course = get_course(session, key)
        account = site_account_for(session, user, course, env_prefix)
        config = course_config(course)
        prefix = account.env_prefix
    criteria = SearchCriteria(
        target_date=date_,
        earliest=time.min,
        latest=time.max,
        players=players,
        holes=holes,
    )
    found = find_tee_times(config, criteria, settings, account_env_prefix=prefix)
    return [TeeTimeOut.from_tee_time(t) for t in found]
