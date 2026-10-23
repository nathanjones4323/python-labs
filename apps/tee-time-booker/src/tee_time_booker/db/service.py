"""What the app does to the database: set things up, claim work, record it.

Every function takes an open `Session` and leaves committing to the caller,
so one unit of work (claim a request *and* open its run) is one transaction.
"""

import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from tee_time_booker import dates
from tee_time_booker.courses import (
    CourseConfig,
    GreenFees,
    ReleaseSchedule,
    Reviews,
    TeeRating,
)
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


class NotFound(LookupError):
    """A lookup by id or key matched nothing."""


# --- courses ---------------------------------------------------------------


def course_config(course: Course) -> CourseConfig:
    """The dataclass the adapter for `course.platform` works with."""
    release = None
    if course.release_opens_at is not None:
        assert course.release_pre_start is not None
        release = ReleaseSchedule(
            opens_at=course.release_opens_at, pre_release_start=course.release_pre_start
        )
    green_fees = None
    if course.green_fees is not None:
        green_fees = GreenFees(
            **{
                **course.green_fees,
                "sampled_on": date.fromisoformat(course.green_fees["sampled_on"]),
            }
        )
    reviews = None
    if course.reviews is not None:
        reviews = Reviews(
            **{
                **course.reviews,
                "sampled_on": date.fromisoformat(course.reviews["sampled_on"]),
            }
        )
    tee_rating = TeeRating(**course.tee_rating) if course.tee_rating else None
    course_type = get_platform(course.platform).course_type
    return course_type(
        key=course.key,
        name=course.name,
        platform=course.platform,
        timezone=ZoneInfo(course.timezone),
        release=release,
        booking_window_days=course.booking_window_days,
        description=course.description,
        green_fees=green_fees,
        reviews=reviews,
        tee_rating=tee_rating,
        latitude=course.latitude,
        longitude=course.longitude,
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
    course.description = config.description
    course.green_fees = None
    if config.green_fees:
        course.green_fees = {
            **asdict(config.green_fees),
            "sampled_on": config.green_fees.sampled_on.isoformat(),
        }
    course.reviews = None
    if config.reviews:
        course.reviews = {
            **asdict(config.reviews),
            "sampled_on": config.reviews.sampled_on.isoformat(),
        }
    course.tee_rating = asdict(config.tee_rating) if config.tee_rating else None
    course.latitude = config.latitude
    course.longitude = config.longitude
    course.platform_config = config.platform_config()
    return course


def get_course(session: Session, key: str) -> Course:
    course = session.scalar(select(Course).where(Course.key == key))
    if course is None:
        known = ", ".join(sorted(session.scalars(select(Course.key))))
        raise NotFound(f"Unknown course {key!r}. Known courses: {known}")
    return course


def list_courses(session: Session) -> list[Course]:
    return list(session.scalars(select(Course).order_by(Course.key)))


# --- users, accounts, plans ------------------------------------------------


def ensure_user(session: Session, email: str, name: str | None = None) -> User:
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, name=name)
        session.add(user)
    elif name:
        user.name = name
    return user


def get_user(session: Session, email: str) -> User:
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        raise NotFound(f"No user {email!r}")
    return user


def get_site_account(session: Session, user: User, env_prefix: str) -> SiteAccount:
    account = session.scalar(
        select(SiteAccount).where(
            SiteAccount.user_id == user.id, SiteAccount.env_prefix == env_prefix
        )
    )
    if account is None:
        raise NotFound(
            f"{user.email} has no site account with env prefix {env_prefix!r}"
        )
    return account


def site_account_for(
    session: Session, user: User, course: Course, env_prefix: str | None = None
) -> SiteAccount:
    """The login that books `course`: the one named by `env_prefix`, or else
    the user's only account on the course's site."""
    if env_prefix:
        account = get_site_account(session, user, env_prefix)
        require_same_platform(course, account)
        return account
    accounts = list(
        session.scalars(
            select(SiteAccount).where(
                SiteAccount.user_id == user.id,
                SiteAccount.platform == course.platform,
            )
        )
    )
    if not accounts:
        raise NotFound(f"{user.email} has no {course.platform} account")
    if len(accounts) > 1:
        prefixes = ", ".join(sorted(a.env_prefix for a in accounts))
        raise ValueError(
            f"{user.email} has several {course.platform} accounts ({prefixes}); "
            "say which with env_prefix"
        )
    return accounts[0]


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


def require_same_platform(course: Course, site_account: SiteAccount) -> None:
    """A login only signs in to the site it belongs to."""
    if site_account.platform != course.platform:
        raise ValueError(
            f"Site account {site_account.env_prefix!r} is a "
            f"{site_account.platform} login; {course.key} is booked through "
            f"{course.platform}"
        )


def ensure_plan(
    session: Session,
    user: User,
    *,
    name: str,
    course: Course,
    site_account: SiteAccount,
    preferences: Preferences,
    auto_book: bool = False,
) -> BookingPlan:
    """The user's plan called `name`, created or updated to `preferences`.

    `auto_book` is set either way, so changing an auto-booked plan turns
    auto-booking off unless the change asks for it again.
    """
    require_same_platform(course, site_account)
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
    plan.auto_book = auto_book
    return plan


def get_plan(session: Session, plan_id: uuid.UUID) -> BookingPlan:
    plan = session.get(BookingPlan, plan_id)
    if plan is None:
        raise NotFound(f"No booking plan {plan_id}")
    return plan


def delete_plan(session: Session, plan_id: uuid.UUID) -> BookingPlan:
    """Delete a plan. Its requests and their runs stay, as one-offs that the
    scheduler no longer books."""
    plan = get_plan(session, plan_id)
    for request in plan.requests:
        request.plan = None
        request.auto_book = False
    session.delete(plan)
    return plan


def list_plans(session: Session, *, user: User | None = None) -> list[BookingPlan]:
    # Eager-load what a listing shows of each plan, not one query per row.
    query = (
        select(BookingPlan)
        .options(
            joinedload(BookingPlan.user),
            joinedload(BookingPlan.course),
            joinedload(BookingPlan.site_account),
        )
        .order_by(BookingPlan.created_at)
    )
    if user is not None:
        query = query.where(BookingPlan.user_id == user.id)
    return list(session.scalars(query))


def plan_target_date(plan: BookingPlan, now: datetime | None = None) -> date:
    """The date this plan books if run now, in the course's local time."""
    return dates.target_date(course_config(plan.course), plan_preferences(plan), now)


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


def create_request(
    session: Session,
    user: User,
    course: Course,
    site_account: SiteAccount,
    criteria: SearchCriteria,
    auto_book: bool = False,
) -> BookingRequest:
    """A one-off request with no plan behind it; `auto_book` schedules it."""
    require_same_platform(course, site_account)
    request = BookingRequest(
        user_id=user.id,
        course_id=course.id,
        site_account_id=site_account.id,
        target_date=criteria.target_date,
        earliest=criteria.earliest,
        latest=criteria.latest,
        holes=criteria.holes,
        players=criteria.players,
        prefer_earlier=criteria.prefer_earlier,
        auto_book=auto_book,
    )
    session.add(request)
    session.flush()
    return request


def get_request(
    session: Session, request_id: uuid.UUID, *, for_update: bool = False
) -> BookingRequest:
    """One request; `for_update` locks its row for a status change."""
    request = session.get(BookingRequest, request_id, with_for_update=for_update)
    if request is None:
        raise NotFound(f"No booking request {request_id}")
    return request


def list_requests(
    session: Session,
    *,
    status: RequestStatus | None = None,
    user: User | None = None,
    limit: int = 50,
) -> list[BookingRequest]:
    query = (
        select(BookingRequest)
        .options(joinedload(BookingRequest.user), joinedload(BookingRequest.course))
        .order_by(BookingRequest.created_at.desc())
    )
    if status is not None:
        query = query.where(BookingRequest.status == status)
    if user is not None:
        query = query.where(BookingRequest.user_id == user.id)
    return list(session.scalars(query.limit(limit)))


def open_requests(session: Session) -> list[BookingRequest]:
    """Every request a run could still be started on (pending or failed),
    with what the scheduler weighs about each loaded up front."""
    query = (
        select(BookingRequest)
        .options(
            joinedload(BookingRequest.course),
            joinedload(BookingRequest.plan),
            joinedload(BookingRequest.runs),
        )
        .where(BookingRequest.status.in_([RequestStatus.PENDING, RequestStatus.FAILED]))
        .order_by(BookingRequest.target_date)
    )
    return list(session.scalars(query).unique())


def cancel_request(session: Session, request_id: uuid.UUID) -> BookingRequest:
    """Cancel a request that isn't running; `InvalidTransition` if it can't be."""
    request = get_request(session, request_id, for_update=True)
    request.transition(RequestStatus.CANCELLED)
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


def claim_request(
    session: Session, request_id: uuid.UUID, *, scheduled: bool = False
) -> ClaimedRun:
    """Mark the request running and open a run for it, under a row lock.

    Two workers claiming the same request at once serialise on the lock; the
    second sees `running` and `transition` refuses it. `scheduled` marks a
    run the scheduler started.
    """
    request = get_request(session, request_id, for_update=True)
    request.transition(RequestStatus.RUNNING)
    run = Run(request=request, scheduled=scheduled)
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
    run = get_run(session, run_id)
    request = get_request(session, run.request_id, for_update=True)
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


def get_run(session: Session, run_id: uuid.UUID) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise NotFound(f"No run {run_id}")
    return run


def recent_runs(session: Session, limit: int = 20) -> list[Run]:
    return list(
        session.scalars(select(Run).order_by(Run.started_at.desc()).limit(limit))
    )
