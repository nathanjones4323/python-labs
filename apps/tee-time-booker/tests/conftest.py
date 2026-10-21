"""Shared fixtures: a migrated test database, wiped between tests.

Database tests run against `TEE_TIME_TEST_DATABASE_URL` when it's set (a
Postgres, see docker-compose.yml) and against a throwaway SQLite file
otherwise, so they run anywhere. The schema is applied with Alembic rather
than `create_all`, so the migrations are exercised too.
"""

import argparse
import os
from datetime import time
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from tee_time_booker import worker
from tee_time_booker.courses import SATICOY, SOULE_PARK
from tee_time_booker.db import Base, make_session_factory
from tee_time_booker.db.service import (
    ensure_plan,
    ensure_site_account,
    ensure_user,
    upsert_course,
)
from tee_time_booker.models import Preferences

TEST_DATABASE_URL = os.environ.get("TEE_TIME_TEST_DATABASE_URL", "").strip()
APP_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def database_url(tmp_path_factory) -> str:
    return TEST_DATABASE_URL or f"sqlite:///{tmp_path_factory.mktemp('db') / 'test.db'}"


@pytest.fixture(scope="session")
def sessions(database_url):
    config = Config(APP_DIR / "alembic.ini")
    config.set_main_option("script_location", str(APP_DIR / "migrations"))
    # env.py reads the URL from -x url=..., which is how the Alembic CLI's
    # own `-x` arrives too.
    config.cmd_opts = argparse.Namespace(x=[f"url={database_url}"])
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    return make_session_factory(database_url)


@pytest.fixture
def db(sessions, database_url, monkeypatch):
    """A session factory over an empty schema; the CLI sees the same database."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    with sessions() as session, session.begin():
        if session.bind.dialect.name == "postgresql":
            tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
            session.execute(text(f"TRUNCATE {tables} CASCADE"))
        else:
            for table in reversed(Base.metadata.sorted_tables):
                session.execute(table.delete())
    return sessions


@pytest.fixture
def seeded(db):
    """Two courses, one user with a login for each, and a plan on each course."""
    with db() as session, session.begin():
        soule = upsert_course(session, SOULE_PARK)
        saticoy = upsert_course(session, SATICOY)
        user = ensure_user(session, "golfer@example.com", "Golfer")
        soule_account = ensure_site_account(
            session,
            user,
            platform="foreup",
            login_email="g@example.com",
            env_prefix="SOULE",
        )
        golfnow_account = ensure_site_account(
            session,
            user,
            platform="golfnow",
            login_email="g@example.com",
            env_prefix="GOLFNOW",
        )
        soule_plan = ensure_plan(
            session,
            user,
            name="soule-saturday",
            course=soule,
            site_account=soule_account,
            preferences=Preferences(
                target_weekday=5,
                weeks_ahead=3,
                earliest=time(8, 0),
                latest=time(14, 0),
                holes=18,
                players=4,
            ),
        )
        saticoy_plan = ensure_plan(
            session,
            user,
            name="saticoy-saturday",
            course=saticoy,
            site_account=golfnow_account,
            preferences=Preferences(
                target_weekday=5,
                weeks_ahead=1,
                earliest=time(7, 0),
                latest=time(11, 0),
                holes=9,
                players=4,
            ),
        )
        session.flush()
        ids = {
            "user": user.id,
            "soule_plan": soule_plan.id,
            "saticoy_plan": saticoy_plan.id,
        }
    return ids


@pytest.fixture
def fake_booking(monkeypatch):
    """`fake_booking(outcome, seen=None)` makes `worker.book_tee_time` return /
    raise `outcome` without a browser; `seen` collects the arguments it got."""

    def install(outcome, seen=None):
        def book_tee_time(
            course, criteria, settings, run_id, *, account_env_prefix, wait_for_release
        ):
            if seen is not None:
                seen.update(
                    course=course,
                    criteria=criteria,
                    settings=settings,
                    run_id=run_id,
                    prefix=account_env_prefix,
                    wait=wait_for_release,
                )
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(worker, "book_tee_time", book_tee_time)

    return install
