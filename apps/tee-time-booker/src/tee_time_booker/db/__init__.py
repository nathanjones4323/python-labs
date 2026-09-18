"""Postgres persistence: who wants what booked, and what every run did.

`models` holds the tables and the request state machine; `service` holds the
operations the CLI (and later an API) perform on them. Connect with
`make_engine(url)` and work inside `Session(engine)` blocks kept short: a
booking run sleeps for up to 95 minutes waiting on a release, and no
connection should be held open across that.
"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from tee_time_booker.db.models import (
    Base,
    BookingPlan,
    BookingRequest,
    Course,
    InvalidTransition,
    RequestStatus,
    Run,
    RunStatus,
    SiteAccount,
    User,
)


def normalize_url(url: str) -> str:
    """SQLAlchemy wants the driver in the scheme; hosted DBs hand out plain `postgresql://`."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def make_engine(url: str) -> Engine:
    # pool_pre_ping: a run's second session may come an hour after its first,
    # long enough for a hosted Postgres to have dropped the idle connection.
    return create_engine(normalize_url(url), pool_pre_ping=True)


def make_session_factory(url: str) -> sessionmaker[Session]:
    return sessionmaker(make_engine(url), expire_on_commit=False)


__all__ = [
    "Base",
    "BookingPlan",
    "BookingRequest",
    "Course",
    "InvalidTransition",
    "RequestStatus",
    "Run",
    "RunStatus",
    "Session",
    "SiteAccount",
    "User",
    "make_engine",
    "make_session_factory",
    "normalize_url",
]
