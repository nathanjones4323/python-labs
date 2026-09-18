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
from tee_time_booker.db import RequestStatus
from tee_time_booker.db.service import (
    cancel_request,
    claim_request,
    create_request,
    get_course,
    get_request,
    get_site_account,
    get_user,
    list_requests,
)
from tee_time_booker.worker import BookingWorker

router = APIRouter(prefix="/requests", tags=["requests"])


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
        return [RequestOut.from_row(r) for r in rows]


@router.post(
    "",
    operation_id="create_request",
    summary="A one-off request: one date at one course, with no plan behind it",
    status_code=status.HTTP_201_CREATED,
)
def create(body: RequestIn, sessions: Sessions) -> RequestOut:
    with sessions() as session, session.begin():
        user = get_user(session, body.user_email)
        course = get_course(session, body.course)
        account = get_site_account(session, user, body.env_prefix)
        request = create_request(session, user, course, account, body.criteria())
        return RequestOut.from_row(request)


@router.get("/{request_id}", operation_id="get_request", summary="One request")
def get(request_id: uuid.UUID, sessions: Sessions) -> RequestOut:
    with sessions() as session:
        return RequestOut.from_row(get_request(session, request_id))


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
)
def cancel(request_id: uuid.UUID, sessions: Sessions) -> RequestOut:
    with sessions() as session, session.begin():
        return RequestOut.from_row(cancel_request(session, request_id))
