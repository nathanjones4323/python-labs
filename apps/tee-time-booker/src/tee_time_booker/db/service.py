"""What the app does to the database: set things up, claim work, record it.

Every function takes an open `Session` and leaves committing to the caller,
so one unit of work (claim a request *and* open its run) is one transaction.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from tee_time_booker.courses import CourseConfig, ReleaseSchedule
from tee_time_booker.db.models import (
    REQUEST_STATUS_AFTER_RUN,
    BookingPlan,
    BookingRequest,
    Course,
    RequestStatus,
    Run,
    RunStatus,
    SiteAccount,
    User,
)
from tee_time_booker.models import BookingResult, Preferences, SearchCriteria
from tee_time_booker.platforms import get_platform

# --- courses ---------------------------------------------------------------


def course_config(course: Course) -> CourseConfig:
    """The dataclass the adapter for `course.platform` works with."""
    release = None
    if course.release_opens_at is not None:
        assert course.release_pre_start is not None
        release = ReleaseSchedule(
            opens_at=course.release_opens_at, pre_release_start=course.release_pre_start
        )
    course_type = get_platform(course.platform).course_type
    return course_type(
        key=course.key,
        name=course.name,
        platform=course.platform,
        timezone=ZoneInfo(course.timezone),
        release=release,
        booking_window_days=course.booking_window_days,
        **course.platform_config,
    )


def upsert_course(session: Session, config: CourseConfig) -> Course:
    """Insert `config`, or bring the row with its key up to date."""
    course = session.scalar(select(Course).where(Course.key == config.key))
    if course is None:
        course = Course(key=config.key)
        session.add(course)
    course.name = config.name
    course.platform = config.platform
    course.timezone = config.timezone.key
    course.release_opens_at = config.release.opens_at if config.release else None
    course.release_pre_start = (
        config.release.pre_release_start if config.release else None
    )
    course.booking_window_days = config.booking_window_days
    course.platform_config = config.platform_config()
    return course


def get_course(session: Session, key: str) -> Course:
    course = session.scalar(select(Course).where(Course.key == key))
    if course is None:
        known = ", ".join(sorted(session.scalars(select(Course.key))))
        raise ValueError(f"Unknown course {key!r}. Known courses: {known}")
    return course


# --- users, accounts, plans ------------------------------------------------


def ensure_user(session: Session, email: str, name: str | None = None) -> User:
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, name=name)
        session.add(user)
    elif name:
        user.name = name
    return user


def ensure_site_account(
    session: Session, user: User, *, platform: str, login_email: str, env_prefix: str
) -> SiteAccount:
    account = session.scalar(
        select(SiteAccount).where(
            SiteAccount.user_id == user.id, SiteAccount.env_prefix == env_prefix
        )
    )
    if account is None:
        account = SiteAccount(user=user, env_prefix=env_prefix)
        session.add(account)
    account.platform = platform
    account.login_email = login_email
    return account


def ensure_plan(
    session: Session,
    user: User,
    *,
    name: str,
    course: Course,
    site_account: SiteAccount,
    preferences: Preferences,
) -> BookingPlan:
    """The user's plan called `name`, created or updated to `preferences`."""
    plan = session.scalar(
        select(BookingPlan).where(
            BookingPlan.user_id == user.id, BookingPlan.name == name
        )
    )
    if plan is None:
        plan = BookingPlan(user=user, name=name)
        session.add(plan)
    plan.course = course
    plan.site_account = site_account
    plan.target_weekday = preferences.target_weekday
    plan.weeks_ahead = preferences.weeks_ahead
    plan.earliest = preferences.earliest
    plan.latest = preferences.latest
    plan.holes = preferences.holes
    plan.players = preferences.players
    plan.prefer_earlier = preferences.prefer_earlier
    return plan


def get_plan(session: Session, plan_id: uuid.UUID) -> BookingPlan:
    plan = session.get(BookingPlan, plan_id)
    if plan is None:
        raise ValueError(f"No booking plan {plan_id}")
    return plan


def plan_preferences(plan: BookingPlan) -> Preferences:
    return Preferences(
        target_weekday=plan.target_weekday,
        weeks_ahead=plan.weeks_ahead,
        earliest=plan.earliest,
        latest=plan.latest,
        holes=plan.holes,
        players=plan.players,
        prefer_earlier=plan.prefer_earlier,
    )


# --- requests and runs -----------------------------------------------------


def request_criteria(request: BookingRequest) -> SearchCriteria:
    return SearchCriteria(
        target_date=request.target_date,
        earliest=request.earliest,
        latest=request.latest,
        players=request.players,
        holes=request.holes,
        prefer_earlier=request.prefer_earlier,
    )


def request_for_plan(
    session: Session, plan: BookingPlan, target_date: date
) -> BookingRequest:
    """The plan's request for `target_date`, created from the plan if it's new."""
    request = session.scalar(
        select(BookingRequest).where(
            BookingRequest.plan_id == plan.id, BookingRequest.target_date == target_date
        )
    )
    if request is None:
        request = BookingRequest(
            user_id=plan.user_id,
            course_id=plan.course_id,
            site_account_id=plan.site_account_id,
            plan=plan,
            target_date=target_date,
            earliest=plan.earliest,
            latest=plan.latest,
            holes=plan.holes,
            players=plan.players,
            prefer_earlier=plan.prefer_earlier,
        )
        session.add(request)
        session.flush()
    return request


def get_request(session: Session, request_id: uuid.UUID) -> BookingRequest:
    request = session.get(BookingRequest, request_id)
    if request is None:
        raise ValueError(f"No booking request {request_id}")
    return request


@dataclass(frozen=True)
class ClaimedRun:
    """Everything a worker needs to drive a run, detached from any session."""

    run_id: uuid.UUID
    request_id: uuid.UUID
    course: CourseConfig
    # `Account.from_env` prefix for the site login.
    account_env_prefix: str
    criteria: SearchCriteria


def claim_request(session: Session, request_id: uuid.UUID) -> ClaimedRun:
    """Mark the request running and open a run for it, under a row lock.

    Two workers claiming the same request at once serialise on the lock; the
    second sees `running` and `transition` refuses it.
    """
    request = session.get(BookingRequest, request_id, with_for_update=True)
    if request is None:
        raise ValueError(f"No booking request {request_id}")
    request.transition(RequestStatus.RUNNING)
    run = Run(request=request)
    session.add(run)
    session.flush()
    return ClaimedRun(
        run_id=run.id,
        request_id=request.id,
        course=course_config(request.course),
        account_env_prefix=request.site_account.env_prefix,
        criteria=request_criteria(request),
    )


def finish_run(
    session: Session,
    run_id: uuid.UUID,
    status: RunStatus,
    *,
    result: BookingResult | None = None,
    detail: str | None = None,
) -> Run:
    """Record how a run ended and move its request on accordingly."""
    run = session.get(Run, run_id)
    if run is None:
        raise ValueError(f"No run {run_id}")
    request = session.get(BookingRequest, run.request_id, with_for_update=True)
    assert request is not None
    run.status = status
    run.finished_at = datetime.now(timezone.utc)
    run.detail = detail
    if result is not None:
        tee_time = result.tee_time
        run.tee_time_local = tee_time.start
        run.side = tee_time.side
        run.green_fee = _money(tee_time.green_fee)
        run.service_fee = _money(tee_time.service_fee)
        run.confirmation = result.confirmation
    request.transition(REQUEST_STATUS_AFTER_RUN[status])
    return run


def _money(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def recent_runs(session: Session, limit: int = 20) -> list[Run]:
    return list(
        session.scalars(select(Run).order_by(Run.started_at.desc()).limit(limit))
    )
