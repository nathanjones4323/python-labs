"""Persistence: who wants what booked, and what every run did.

Postgres for a hosted install (and CI); SQLite for someone running it on
their own machine, so a local install needs no database server.

`models` holds the tables and the request state machine; `service` holds the
operations the CLI (and later an API) perform on them. Connect with
`make_engine(url)` and work inside `Session(engine)` blocks kept short: a
booking run sleeps for up to 95 minutes waiting on a release, and no
connection should be held open across that.
"""

from sqlalchemy import Engine, create_engine, event
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
    url = normalize_url(url)
    if url.startswith("sqlite"):
        return _sqlite_engine(url)
    # pool_pre_ping: a run's second session may come an hour after its first,
    # long enough for a hosted Postgres to have dropped the idle connection.
    return create_engine(url, pool_pre_ping=True)


def _sqlite_engine(url: str) -> Engine:
    """SQLite, made to keep the promises Postgres keeps for free.

    Claiming a request locks its row (`with_for_update`), which SQLite
    ignores. Starting every transaction with BEGIN IMMEDIATE takes the
    database's write lock up front instead, so two claims of one request
    queue behind each other rather than both reading `pending`. Foreign keys
    are off in SQLite unless asked for, and WAL lets reads carry on while a
    write is open. The API's worker threads share the engine, hence
    check_same_thread=False and a timeout to wait out each other's locks.
    """
    engine = create_engine(
        url, connect_args={"check_same_thread": False, "timeout": 30}
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:
        # Leave BEGIN to SQLAlchemy (below) rather than pysqlite's own.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


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
