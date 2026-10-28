"""Persistence: a SQLite file on this machine, migrated with Alembic.

Connect with `make_session_factory(url)`; `migrate(url)` brings the schema to
head, which the watcher and the MCP server do at start-up, so a fresh
install needs no extra step.
"""

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from campsite_booker.db.models import (
    BOOKED,
    BY_EMAIL,
    BY_POLL,
    BY_RELEASE,
    CHALLENGED,
    DRY_RUN,
    EMAIL_FIRST,
    EXPIRED,
    FAILED,
    HELD,
    MISSED,
    NOT_WATCHED,
    PAID,
    STOPPED,
    UNKNOWN,
    WATCHER_FIRST,
    AlertEmail,
    Base,
    Hold,
    Opening,
    Watch,
    utcnow,
)

APP_DIR = Path(__file__).resolve().parents[3]


def make_engine(url: str) -> Engine:
    if not url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True)
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    # The watcher works the database from worker threads (`asyncio.to_thread`).
    engine = create_engine(
        url, connect_args={"check_same_thread": False, "timeout": 30}
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def make_session_factory(url: str) -> sessionmaker[Session]:
    return sessionmaker(make_engine(url), expire_on_commit=False)


def alembic_config(url: str) -> Config:
    config = Config(APP_DIR / "alembic.ini")
    config.set_main_option("script_location", str(APP_DIR / "migrations"))
    # env.py reads the URL from -x url=..., as the Alembic CLI passes it.
    config.cmd_opts = argparse.Namespace(x=[f"url={url}"])
    return config


def migrate(url: str) -> None:
    make_engine(url).dispose()  # creates the SQLite file's directory
    command.upgrade(alembic_config(url), "head")


__all__ = [
    "BOOKED",
    "BY_EMAIL",
    "BY_POLL",
    "BY_RELEASE",
    "CHALLENGED",
    "EMAIL_FIRST",
    "MISSED",
    "NOT_WATCHED",
    "WATCHER_FIRST",
    "AlertEmail",
    "DRY_RUN",
    "EXPIRED",
    "FAILED",
    "HELD",
    "PAID",
    "STOPPED",
    "UNKNOWN",
    "Base",
    "Hold",
    "Opening",
    "Session",
    "Watch",
    "alembic_config",
    "make_engine",
    "make_session_factory",
    "migrate",
    "utcnow",
]
