"""Shared fixtures: settings, a migrated SQLite file wiped between tests, the
real (trimmed) recreation.gov responses in tests/fixtures, and a fake
recreation.gov whose availability a test sets.

The schema is applied with Alembic rather than `create_all`, so the
migration is exercised too.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from campsite_booker.config import Settings
from campsite_booker.db import Base, alembic_config, make_session_factory
from campsite_booker.gmail import Message
from campsite_booker.recgov import (
    AVAILABLE,
    Campground,
    Campsite,
    RecGovError,
    parse_month,
)

FIXTURES = Path(__file__).parent / "fixtures"
SCORPION = "232498"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def scorpion_november() -> list[Campsite]:
    """Four real sites at Santa Cruz Island Scorpion, Nov 1-21 2026.

    Nights (A = Available, R = Reserved), from Nov 1:
        022 (4924)  AARAARRRRRRARRRRRARRR  tent, 6 people
        020 (4925)  RRAARRRRRRRARRRRAAARR  tent, 6 people
        023 (4926)  RRRRRRRRRRRRRRARRAARR  tent, 6 people
        A   (4939)  AAAAARRRAAAARRAAAARRR  group tent, 15 people
    """
    return parse_month(fixture("availability_232498_2026-11.json"))


def site(campsite_id: str, free: list[date], **kw) -> Campsite:
    """A made-up site, Available on `free` and Reserved the rest of Nov."""
    nights = {date(2026, 11, 1) + timedelta(days=i): "Reserved" for i in range(30)}
    nights.update({d: AVAILABLE for d in free})
    return Campsite(
        campsite_id=campsite_id,
        site=kw.get("site", campsite_id),
        loop=kw.get("loop", "Loop A"),
        campsite_type=kw.get("campsite_type", "STANDARD NONELECTRIC"),
        type_of_use="Overnight",
        max_people=kw.get("max_people", 6),
        min_people=0,
        nights=nights,
    )


class FakeRecGov:
    """Stands in for `Campgrounds` (both sites): `sites[campground_id]` is
    what availability returns; `fail` makes a campground's fetch raise."""

    def __init__(self) -> None:
        self.sites: dict[str, list[Campsite]] = {}
        self.fail: set[str] = set()
        self.calls: list[tuple[str, date, date]] = []
        # Raw release schedules by campground; none scheduled by default.
        self.release_payloads: dict[str, dict] = {}
        self.release_calls: list[str] = []

    def availability(self, campground_id: str, first: date, last: date):
        self.calls.append((campground_id, first, last))
        if campground_id in self.fail:
            raise RecGovError("recreation.gov answered 503")
        return {s.campsite_id: s for s in self.sites.get(campground_id, [])}

    def releases(self, campground_id: str) -> dict:
        self.release_calls.append(campground_id)
        return self.release_payloads.get(campground_id, {})

    def campground_of(self, campsite_id: str) -> str:
        for campground_id, sites in self.sites.items():
            if any(s.campsite_id == campsite_id for s in sites):
                return campground_id
        raise RecGovError(f"No campsite {campsite_id} on recreation.gov")

    def campground(self, campground_id: str) -> Campground:
        return Campground(id=campground_id, name=f"Campground {campground_id}")

    def search(self, query: str, limit: int = 20) -> list[Campground]:
        return [self.campground(cid) for cid in self.sites][:limit]


class FakeMailbox:
    """Stands in for `gmail.Gmail`: `messages` is the inbox."""

    def __init__(self) -> None:
        self.messages: dict[str, Message] = {}

    def recent_ids(self, days: int = 1) -> list[str]:
        return list(self.messages)

    def fetch(self, gmail_id: str) -> Message:
        return self.messages[gmail_id]


def alert_message(gmail_id: str, received_at: datetime) -> Message:
    """The real Upper Pines alert (site 037, campsite 94, "as of" 4:03 PM
    PDT on Oct 27 2026), received at `received_at`."""
    html = (FIXTURES / "alert_campsite_available.html").read_text()
    return Message(gmail_id, "Campsite Available!", received_at, html)


@pytest.fixture
def recgov() -> FakeRecGov:
    return FakeRecGov()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'unused.db'}",
        recgov_email=None,
        recgov_password=None,
        session_file=tmp_path / "session.json",
        artifacts_dir=tmp_path / "artifacts",
        headless=True,
        timezone=ZoneInfo("America/Los_Angeles"),
    )


@pytest.fixture(scope="session")
def sessions(tmp_path_factory):
    url = f"sqlite:///{tmp_path_factory.mktemp('db') / 'test.db'}"
    config = alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    return make_session_factory(url)


@pytest.fixture
def db(sessions):
    """A session factory over an empty schema."""
    with sessions.begin() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
    return sessions
