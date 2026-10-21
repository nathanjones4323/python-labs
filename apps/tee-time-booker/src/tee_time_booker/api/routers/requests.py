"""Booking requests: the unit of work, and starting a run on one.

`start_run` is the one place a run begins, whether from a plan or from a
request directly: claim the request in a transaction of its own (so
"already running" or "already booked" is a 409 to the caller, not a run
that fails later), then hand it to the worker and answer 202 with the run
id to poll.
"""

import uuid

from fastapi import APIRouter, Query, status
from sqlalchemy.orm import Session, sessionmaker

from tee_time_booker.api.deps import Sessions, Worker
from tee_time_booker.api.schemas import (
    RequestIn,
    RequestOut,
    RunOptions,
    RunOut,
    RunStarted,
)
from tee_time_booker.db import BookingRequest, RequestStatus
from tee_time_booker.db.service import (
    cancel_request,
    claim_request,
    course_config,
    create_request,
    get_course,
    get_request,
    get_user,
    list_requests,
    site_account_for,
)
from tee_time_booker.scheduler import first_attempt
from tee_time_booker.worker import BookingWorker

router = APIRouter(prefix="/requests", tags=["requests"])


def _out(request: BookingRequest) -> RequestOut:
    first = None
    if request.armed:
        first = first_attempt(course_config(request.course), request.target_date)
    return RequestOut.from_row(request, first)


def start_run(
    sessions: sessionmaker[Session],
    worker: BookingWorker,
    request_id: uuid.UUID,
    options: RunOptions,
) -> RunStarted:
    with sessions() as session, session.begin():
        claimed = claim_request(session, request_id)
    worker.start(
        claimed,
        submit_payment=options.submit_payment,
        wait_for_release=options.wait_for_release,
    )
    return RunStarted(
        run_id=claimed.run_id,
        request_id=claimed.request_id,
        course=claimed.course.key,
        target_date=claimed.criteria.target_date,
        dry_run=not worker.pays(options.submit_payment),
        wait_for_release=options.wait_for_release,
    )


@router.get("", operation_id="list_requests", summary="Booking requests, newest first")
def list_(
    sessions: Sessions,
    status_: RequestStatus | None = Query(default=None, alias="status"),
    user_email: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[RequestOut]:
    with sessions() as session:
        user = get_user(session, user_email) if user_email else None
        rows = list_requests(session, status=status_, user=user, limit=limit)
        return [_out(r) for r in rows]


@router.post(
    "",
    operation_id="create_request",
    summary="A one-off request: one date at one course, with no plan behind it",
    description=(
        "Run it now with run_request, or set auto_book to have the scheduler "
        "book it as soon as the day can be booked."
    ),
    status_code=status.HTTP_201_CREATED,
)
def create(body: RequestIn, sessions: Sessions) -> RequestOut:
    with sessions() as session, session.begin():
        user = get_user(session, body.user_email)
        course = get_course(session, body.course)
        account = site_account_for(session, user, course, body.env_prefix)
        request = create_request(
            session, user, course, account, body.criteria(), auto_book=body.auto_book
        )
        return _out(request)


@router.get("/{request_id}", operation_id="get_request", summary="One request")
def get(request_id: uuid.UUID, sessions: Sessions) -> RequestOut:
    with sessions() as session:
        return _out(get_request(session, request_id))


@router.get(
    "/{request_id}/runs",
    operation_id="list_request_runs",
    summary="Every attempt at this request, oldest first",
)
def runs(request_id: uuid.UUID, sessions: Sessions) -> list[RunOut]:
    with sessions() as session:
        return [RunOut.from_row(r) for r in get_request(session, request_id).runs]


@router.post(
    "/{request_id}/run",
    operation_id="run_request",
    summary="Start a run on this request (pending or failed) in the background",
    status_code=status.HTTP_202_ACCEPTED,
)
def run(
    request_id: uuid.UUID, body: RunOptions, sessions: Sessions, worker: Worker
) -> RunStarted:
    return start_run(sessions, worker, request_id, body)


@router.post(
    "/{request_id}/cancel",
    operation_id="cancel_request",
    summary="Cancel a request that isn't running or booked",
    description="Also how a scheduled one-off is called off.",
)
def cancel(request_id: uuid.UUID, sessions: Sessions) -> RequestOut:
    with sessions() as session, session.begin():
        return _out(cancel_request(session, request_id))
