from fastapi import APIRouter

from tee_time_booker.api.deps import Sessions
from tee_time_booker.api.schemas import CourseOut
from tee_time_booker.db.service import get_course, list_courses

router = APIRouter(prefix="/courses", tags=["courses"])


@router.get("", operation_id="list_courses", summary="The bookable courses")
def list_(sessions: Sessions) -> list[CourseOut]:
    with sessions() as session:
        return [CourseOut.from_row(c) for c in list_courses(session)]


@router.get("/{key}", operation_id="get_course", summary="One course by key")
def get(key: str, sessions: Sessions) -> CourseOut:
    with sessions() as session:
        return CourseOut.from_row(get_course(session, key))
