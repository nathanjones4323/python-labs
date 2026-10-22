"""Shared fixtures: settings, a migrated SQLite file wiped between tests, and
the real (redacted) bill emails in tests/fixtures.

The schema is applied with Alembic rather than `create_all`, so the
migrations are exercised too.
"""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from pay_my_bills.config import Settings
from pay_my_bills.db import Base, alembic_config, make_session_factory
from pay_my_bills.parsers import Email

FIXTURES = Path(__file__).parent / "fixtures"
KUBRA = "noreply@kubra.com"
SCE = "sce@message.sce.com"
VENMO = "Venmo <venmo@venmo.com>"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def at(text: str) -> datetime:
    """`2026-10-14 15:49` Pacific (PDT), as an aware UTC datetime."""
    local = datetime.fromisoformat(text).replace(tzinfo=ZoneInfo("America/Los_Angeles"))
    return local.astimezone(UTC)


def email(gmail_id: str, sender: str, subject: str, received: str, text: str = ""):
    return Email(
        gmail_id=gmail_id,
        sender=sender,
        subject=subject,
        received_at=at(received),
        text=text,
    )


def water_bill(gmail_id: str = "w-bill", received: str = "2026-10-07 10:53") -> Email:
    return email(
        gmail_id,
        KUBRA,
        "Do Not Reply: Your Ventura Water eBill is Now Available",
        received,
        fixture("water_bill_ready.txt"),
    )


def water_paid(gmail_id: str = "w-paid", received: str = "2026-10-10 21:33") -> Email:
    return email(
        gmail_id,
        KUBRA,
        "Ventura Water Payment Service- Payment Confirmation",
        received,
        fixture("water_payment_confirmation.txt"),
    )


def sce_bill(gmail_id: str = "e-bill", received: str = "2026-10-14 15:49") -> Email:
    return email(
        gmail_id, SCE, "Bill is Ready", received, fixture("sce_bill_ready.txt")
    )


def venmo_paid(amount: str, gmail_id: str = "v-paid", payer: str = "Austin Will"):
    return email(
        gmail_id,
        VENMO,
        f"{payer} paid your ${amount} request",
        "2026-10-15 09:00",
    )


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'unused.db'}",
        roommate_name="Austin Will",
        roommate_venmo="AGWill",
        roommate_zelle=None,
        split_percent=50,
        electric_autopay_lag_days=10,
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
