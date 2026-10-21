"""The tables, and the state machine a booking request moves through.

    users ─┬─ site_accounts     a login on one booking site (secret in the env)
           └─ booking_plans     a standing wish: this course, this weekday, ...
                 └─ booking_requests   that wish pinned to one date
                       └─ runs         one attempt to satisfy a request

    courses                      where and how a site releases days

A `BookingRequest` is the unit of work. It starts `pending`; a worker moves it
to `running` while it drives the site, and the outcome of that `Run` moves it
on to `booked`, `failed` or `unknown` — or back to `pending` after a dry run,
which fulfils nothing. `BookingRequest.transition` refuses any other move, so
a request can't be booked twice or resurrected once cancelled.
"""

import enum
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    TypeDecorator,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UTCDateTime(TypeDecorator[datetime]):
    """A timestamp that always comes back timezone-aware.

    Postgres's `timestamptz` does this itself. SQLite has no timezone type:
    values go in as naive UTC (its `CURRENT_TIMESTAMP` default is UTC too) and
    get their tzinfo back on the way out, so callers never see a naive one.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Any
    ) -> datetime | None:
        if value is not None and value.tzinfo is not None and dialect.name == "sqlite":
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(
        self, value: datetime | None, dialect: Any
    ) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: UTCDateTime(),
        # JSONB on Postgres; SQLite (a local install) stores it as text.
        dict[str, Any]: JSON().with_variant(JSONB, "postgresql"),
    }


def _enum(py_enum: type[enum.Enum], name: str) -> Enum:
    # Store the lowercase values, not the member names, so `psql` reads well.
    return Enum(py_enum, name=name, values_callable=lambda e: [m.value for m in e])


class RequestStatus(enum.StrEnum):
    PENDING = "pending"  # waiting for a worker
    RUNNING = "running"  # a worker is on the site now
    BOOKED = "booked"  # paid and confirmed
    FAILED = "failed"  # the run raised; may be retried
    UNKNOWN = "unknown"  # payment sent, no confirmation seen: check the site
    CANCELLED = "cancelled"


# Every move a request may make. Terminal states have no entry.
REQUEST_TRANSITIONS: dict[RequestStatus, frozenset[RequestStatus]] = {
    RequestStatus.PENDING: frozenset({RequestStatus.RUNNING, RequestStatus.CANCELLED}),
    RequestStatus.RUNNING: frozenset(
        {
            RequestStatus.PENDING,  # a dry run: nothing was booked
            RequestStatus.BOOKED,
            RequestStatus.FAILED,
            RequestStatus.UNKNOWN,
        }
    ),
    RequestStatus.FAILED: frozenset({RequestStatus.RUNNING, RequestStatus.CANCELLED}),
    # Only a person can say what an unknown payment came to.
    RequestStatus.UNKNOWN: frozenset(
        {RequestStatus.BOOKED, RequestStatus.FAILED, RequestStatus.CANCELLED}
    ),
    RequestStatus.BOOKED: frozenset(),
    RequestStatus.CANCELLED: frozenset(),
}


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    DRY_RUN = "dry_run"  # reached checkout, stopped before paying
    ERROR = "error"
    UNKNOWN = "unknown"  # submit clicked, no confirmation or error shown


# What a finished run does to its request.
REQUEST_STATUS_AFTER_RUN: dict[RunStatus, RequestStatus] = {
    RunStatus.SUCCESS: RequestStatus.BOOKED,
    RunStatus.DRY_RUN: RequestStatus.PENDING,
    RunStatus.ERROR: RequestStatus.FAILED,
    RunStatus.UNKNOWN: RequestStatus.UNKNOWN,
}


class InvalidTransition(RuntimeError):
    """A request was asked to move somewhere its state machine forbids."""


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    site_accounts: Mapped[list["SiteAccount"]] = relationship(back_populates="user")
    plans: Mapped[list["BookingPlan"]] = relationship(back_populates="user")


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    platform: Mapped[str] = mapped_column(String(50))
    timezone: Mapped[str] = mapped_column(String(64))  # IANA name
    # See `courses.ReleaseSchedule`; both set or both null.
    release_opens_at: Mapped[time | None] = mapped_column(Time)
    release_pre_start: Mapped[time | None] = mapped_column(Time)
    booking_window_days: Mapped[int | None] = mapped_column(SmallInteger)
    # The adapter's own identifiers (`CourseConfig.platform_config()`).
    platform_config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SiteAccount(Base):
    """A user's login on one booking site.

    The password (and, for now, the email too) live in the environment under
    `<env_prefix>_EMAIL` / `<env_prefix>_PASSWORD`; `login_email` is only so
    the account can be recognised in listings.
    """

    __tablename__ = "site_accounts"
    __table_args__ = (UniqueConstraint("user_id", "env_prefix"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(50))
    login_email: Mapped[str] = mapped_column(String(320))
    env_prefix: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    user: Mapped[User] = relationship(back_populates="site_accounts")


class BookingPlan(Base):
    """A standing wish: book this course, this weekday, this far ahead.

    The columns from `target_weekday` on are a `models.Preferences`.
    """

    __tablename__ = "booking_plans"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"))
    site_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("site_accounts.id"))
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    target_weekday: Mapped[int] = mapped_column(SmallInteger)
    weeks_ahead: Mapped[int] = mapped_column(SmallInteger)
    earliest: Mapped[time] = mapped_column(Time)
    latest: Mapped[time] = mapped_column(Time)
    holes: Mapped[int] = mapped_column(SmallInteger)
    players: Mapped[int] = mapped_column(SmallInteger)
    prefer_earlier: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    user: Mapped[User] = relationship(back_populates="plans")
    course: Mapped[Course] = relationship()
    site_account: Mapped[SiteAccount] = relationship()
    requests: Mapped[list["BookingRequest"]] = relationship(back_populates="plan")


class BookingRequest(Base):
    """One plan pinned to one date — or a one-off with no plan behind it.

    The search columns are copied from the plan at creation so editing a plan
    never changes a request already in flight.
    """

    __tablename__ = "booking_requests"
    # One request per plan per day; one-offs (plan_id null) never collide.
    __table_args__ = (UniqueConstraint("plan_id", "target_date"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"))
    site_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("site_accounts.id"))
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("booking_plans.id", ondelete="SET NULL")
    )
    target_date: Mapped[date] = mapped_column(Date)
    earliest: Mapped[time] = mapped_column(Time)
    latest: Mapped[time] = mapped_column(Time)
    holes: Mapped[int] = mapped_column(SmallInteger)
    players: Mapped[int] = mapped_column(SmallInteger)
    prefer_earlier: Mapped[bool] = mapped_column(Boolean, default=True)
    # Indexed: a worker's first question is "what's pending?".
    status: Mapped[RequestStatus] = mapped_column(
        _enum(RequestStatus, "request_status"),
        default=RequestStatus.PENDING,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship()
    course: Mapped[Course] = relationship()
    site_account: Mapped[SiteAccount] = relationship()
    plan: Mapped[BookingPlan | None] = relationship(back_populates="requests")
    runs: Mapped[list["Run"]] = relationship(
        back_populates="request", order_by="Run.started_at"
    )

    def transition(self, to: RequestStatus) -> None:
        """Move to `to`, or raise if the state machine doesn't allow it."""
        if to not in REQUEST_TRANSITIONS[self.status]:
            raise InvalidTransition(
                f"Booking request {self.id} cannot go from {self.status} to {to}"
            )
        self.status = to


class Run(Base):
    """One attempt at a request: what it found, what it did, what it cost."""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("booking_requests.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "run_status"), default=RunStatus.RUNNING
    )
    started_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None]
    # A short error message; the traceback belongs in the job log.
    detail: Mapped[str | None] = mapped_column(Text)
    # The slot that was held (naive, course-local), when the run got that far.
    tee_time_local: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    side: Mapped[str | None] = mapped_column(String(20))
    green_fee: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    service_fee: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    confirmation: Mapped[str | None] = mapped_column(Text)

    request: Mapped[BookingRequest] = relationship(back_populates="runs")
