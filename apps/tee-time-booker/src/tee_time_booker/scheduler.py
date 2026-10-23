"""Booking what's been scheduled, as soon as its day can be booked.

An auto-booked plan or one-off request is *armed*: the person confirmed once,
when they scheduled it, that it may book and pay without asking again. Each
tick (`start_due`) looks over the open requests, picks the ones that are due
and starts a run on each through the same worker a booking from a chat uses.
The bot calls it every minute; nothing else does, so bookings are only
scheduled while the bot is running.

When a request is due depends on how its course releases days:

- A fixed release (foreUP's midnight drop, a known window): the day opens at
  the release `booking_window_days` before it. The run starts `START_LEAD`
  ahead of that release and waits for it, or at once if the day is already
  open.
- A rolling window (GolfNow), which the site only reveals once signed in:
  one try a morning, from `ROLLING_HORIZON_DAYS` out. A try that finds the day
  still beyond the window is a `not_open` run, and the next morning tries
  again.

A request gets one real scheduled attempt. Once a scheduled run has done
anything but find the day not open, the scheduler leaves the request alone
whatever came of it, so a failure is reported rather than retried in a loop.
Nor does it try twice in one course-local day.

An armed plan's request for its next date is created here, the first tick the
plan's target moves on to it, so it shows in listings from then on.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tee_time_booker.courses import CourseConfig
from tee_time_booker.db import BookingPlan, BookingRequest, InvalidTransition, RunStatus
from tee_time_booker.db.service import (
    ClaimedRun,
    claim_request,
    course_config,
    open_requests,
    plan_target_date,
    request_for_plan,
)
from tee_time_booker.worker import BookingWorker

# How long before a fixed release a run starts: time to sign in and be ready.
START_LEAD = timedelta(minutes=10)
# Course-local time of the daily try at a rolling-window course.
ROLLING_ATTEMPT_AT = time(6, 0)
# How far out a rolling-window course is first tried. GolfNow facilities list
# anywhere from a week to about four weeks ahead.
ROLLING_HORIZON_DAYS = 30


def first_attempt(course: CourseConfig, target: date) -> datetime:
    """When the scheduler starts trying to book `target` at `course`."""
    if course.release is not None and course.booking_window_days is not None:
        opens_on = target - timedelta(days=course.booking_window_days)
        opens = datetime.combine(
            opens_on, course.release.opens_at, tzinfo=course.timezone
        )
        return opens - START_LEAD
    first_day = target - timedelta(days=ROLLING_HORIZON_DAYS)
    return datetime.combine(first_day, ROLLING_ATTEMPT_AT, tzinfo=course.timezone)


@dataclass(frozen=True)
class Due:
    request_id: uuid.UUID
    wait_for_release: bool


def due_attempt(request: BookingRequest, now: datetime) -> Due | None:
    """Whether the scheduler should start a run on `request` at `now`."""
    if not request.armed:
        return None
    course = course_config(request.course)
    local = now.astimezone(course.timezone)
    if request.target_date < local.date():
        return None
    scheduled = [r for r in request.runs if r.scheduled]
    if any(r.status != RunStatus.NOT_OPEN for r in scheduled):
        return None
    if any(
        r.started_at.astimezone(course.timezone).date() == local.date()
        for r in scheduled
    ):
        return None
    start = first_attempt(course, request.target_date)
    if now < start:
        return None
    if course.release is not None and course.booking_window_days is not None:
        return Due(request.id, wait_for_release=now < start + START_LEAD)
    if local.time() < ROLLING_ATTEMPT_AT:
        return None
    return Due(request.id, wait_for_release=False)


def arm_plans(session: Session, now: datetime) -> None:
    """Make sure every auto-booked plan has a request for its next date."""
    for plan in session.scalars(select(BookingPlan).where(BookingPlan.auto_book)):
        request_for_plan(session, plan, plan_target_date(plan, now))


@dataclass(frozen=True)
class Started:
    claimed: ClaimedRun
    dry_run: bool


def start_due(
    sessions: sessionmaker[Session],
    worker: BookingWorker,
    now: datetime | None = None,
) -> list[Started]:
    """Start a run on every armed request that's due; what was started."""
    now = now or datetime.now(timezone.utc)
    with sessions() as session, session.begin():
        arm_plans(session, now)
        session.flush()
        due = [d for r in open_requests(session) if (d := due_attempt(r, now))]
    started = []
    for item in due:
        try:
            with sessions() as session, session.begin():
                claimed = claim_request(
                    session, item.request_id, scheduled=True, started_at=now
                )
        except InvalidTransition:
            continue  # started some other way since it was looked at
        try:
            worker.start(
                claimed, submit_payment=True, wait_for_release=item.wait_for_release
            )
        except RuntimeError:
            # The worker is shutting down; `start` recorded the run as an error.
            logger.warning("Scheduler: the worker stopped taking runs")
            break
        logger.info(
            f"Scheduler: run {claimed.run_id} for {claimed.course.name} on "
            f"{claimed.criteria.target_date}"
        )
        started.append(Started(claimed, dry_run=not worker.pays(True)))
    return started
