"""Booking plans, and the two things a plan is for: looking, and booking."""

import uuid
from datetime import date

from fastapi import APIRouter, Query, status

from tee_time_booker import dates
from tee_time_booker.api.deps import AppSettings, Sessions, Worker
from tee_time_booker.api.routers.requests import start_run
from tee_time_booker.api.schemas import (
    BookIn,
    PlanIn,
    PlanOut,
    RunStarted,
    TeeTimeOut,
)
from tee_time_booker.db import BookingPlan
from tee_time_booker.db.service import (
    course_config,
    ensure_plan,
    get_course,
    get_plan,
    get_site_account,
    get_user,
    list_plans,
    plan_preferences,
    request_for_plan,
)
from tee_time_booker.runner import find_tee_times

router = APIRouter(prefix="/plans", tags=["plans"])


def _out(plan: BookingPlan) -> PlanOut:
    course = course_config(plan.course)
    return PlanOut.from_row(plan, dates.target_date(course, plan_preferences(plan)))


@router.get(
    "", operation_id="list_plans", summary="Booking plans, optionally one user's"
)
def list_(sessions: Sessions, user_email: str | None = None) -> list[PlanOut]:
    with sessions() as session:
        user = get_user(session, user_email) if user_email else None
        return [_out(p) for p in list_plans(session, user=user)]


@router.post(
    "",
    operation_id="create_plan",
    summary="Create a plan, or update the user's plan of that name",
    status_code=status.HTTP_201_CREATED,
)
def create(body: PlanIn, sessions: Sessions) -> PlanOut:
    with sessions() as session, session.begin():
        user = get_user(session, body.user_email)
        course = get_course(session, body.course)
        account = get_site_account(session, user, body.env_prefix)
        if account.platform != course.platform:
            raise ValueError(
                f"Site account {body.env_prefix!r} is a {account.platform} login; "
                f"{course.key} is booked through {course.platform}"
            )
        plan = ensure_plan(
            session,
            user,
            name=body.name,
            course=course,
            site_account=account,
            preferences=body.preferences(),
        )
        session.flush()
        return _out(plan)


@router.get("/{plan_id}", operation_id="get_plan", summary="One plan")
def get(plan_id: uuid.UUID, sessions: Sessions) -> PlanOut:
    with sessions() as session:
        return _out(get_plan(session, plan_id))


@router.get(
    "/{plan_id}/tee-times",
    operation_id="find_tee_times",
    summary="What's bookable for this plan, without booking anything",
    description=(
        "Signs in to the course's site and searches the plan's target date "
        "(or `date`) for slots fitting its holes and group size, then closes "
        "the browser. Takes up to a minute or two; the site login must be "
        "configured but no card details are needed. Returns every slot on the "
        "day, not only those in the plan's window."
    ),
)
def tee_times(
    plan_id: uuid.UUID,
    sessions: Sessions,
    settings: AppSettings,
    date_: date | None = Query(default=None, alias="date"),
) -> list[TeeTimeOut]:
    with sessions() as session:
        plan = get_plan(session, plan_id)
        course = course_config(plan.course)
        prefs = plan_preferences(plan)
        target = date_ or dates.target_date(course, prefs)
        criteria = prefs.criteria(target)
        env_prefix = plan.site_account.env_prefix
    found = find_tee_times(course, criteria, settings, account_env_prefix=env_prefix)
    return [TeeTimeOut.from_tee_time(t) for t in found]


@router.post(
    "/{plan_id}/book",
    operation_id="book_plan",
    summary="Book this plan's next date (or `target_date`) in the background",
    description=(
        "Finds or creates the plan's request for the date, claims it and starts "
        "a run. Answers at once with the run id; poll `get_run` for the outcome. "
        "A run may wait up to 95 minutes for the course's release. 409 if the "
        "request for that date is already running or booked."
    ),
    status_code=status.HTTP_202_ACCEPTED,
)
def book(
    plan_id: uuid.UUID, body: BookIn, sessions: Sessions, worker: Worker
) -> RunStarted:
    with sessions() as session, session.begin():
        plan = get_plan(session, plan_id)
        course = course_config(plan.course)
        target = body.target_date or dates.target_date(course, plan_preferences(plan))
        request_id = request_for_plan(session, plan, target).id
    return start_run(sessions, worker, request_id, body)
