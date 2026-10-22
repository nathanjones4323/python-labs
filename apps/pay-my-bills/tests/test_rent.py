"""Rent: half of $2,900 asked for at 9am on the last day of each month, for
the month after, with the same buttons, reminders and settling as a bill."""

import asyncio
import dataclasses

import pytest
from conftest import at, venmo_paid
from pay_my_bills import service
from pay_my_bills.config import Settings
from pay_my_bills.db import RENT, SCHEDULED, Payment, SplitRequest
from test_bot import FakeBot, make_bot
from test_pipeline import FakeMailbox, count, poll, requests, send_all


@pytest.fixture
def rent(settings) -> Settings:
    return dataclasses.replace(settings, rent_total_cents=290000)


def test_rent_is_asked_for_at_9am_on_the_last_day(db, rent):
    mailbox = FakeMailbox()
    poll(db, rent, mailbox, "2026-10-31 08:59")
    assert count(db, SplitRequest) == 0

    poll(db, rent, mailbox, "2026-10-31 09:00")
    [request] = requests(db)
    assert request.share_cents == 145000
    assert request.note == "Rent November 2026"
    assert request.payment.utility == RENT
    assert request.payment.source == SCHEDULED

    [item] = send_all(db, rent, "2026-10-31 09:00")
    assert item.text.startswith("🏠 <b>Rent November 2026</b>: $2,900.00")
    assert "Austin Will owes <b>$1,450.00</b> (50%)" in item.text
    assert "amount=1450.00" in item.venmo_url
    assert "note=Rent%20November%202026" in item.venmo_url


def test_rent_is_asked_for_once_a_month(db, rent):
    mailbox = FakeMailbox()
    for when in ("2026-10-31 09:00", "2026-10-31 18:00", "2026-11-02 09:00"):
        poll(db, rent, mailbox, when)
    assert count(db, Payment) == 1
    poll(db, rent, mailbox, "2026-11-30 09:00")
    assert [r.note for r in requests(db)] == [
        "Rent November 2026",
        "Rent December 2026",
    ]


def test_december_rent_is_for_january(db, rent):
    poll(db, rent, FakeMailbox(), "2026-12-31 09:00")
    [request] = requests(db)
    assert request.note == "Rent January 2027"


def test_short_months(db, rent):
    poll(db, rent, FakeMailbox(), "2027-02-27 12:00")
    assert count(db, SplitRequest) == 0
    poll(db, rent, FakeMailbox(), "2027-02-28 09:00")
    [request] = requests(db)
    assert request.note == "Rent March 2027"


def test_bot_off_on_the_day_catches_up_within_a_week(db, rent):
    poll(db, rent, FakeMailbox(), "2026-11-03 10:00")
    [request] = requests(db)
    assert request.note == "Rent November 2026"


def test_but_not_a_month_long_since_paid(db, rent):
    # A fresh install on Oct 22 mustn't ask for October again (charged Sep 30).
    poll(db, rent, FakeMailbox(), "2026-10-22 12:00")
    poll(db, rent, FakeMailbox(), "2026-11-07 09:00")
    assert count(db, SplitRequest) == 0


def test_rent_off_without_a_total(db, settings):
    poll(db, settings, FakeMailbox(), "2026-10-31 09:00")
    assert count(db, SplitRequest) == 0


def test_paying_the_rent_request_settles_it(db, rent):
    mailbox = FakeMailbox()
    poll(db, rent, mailbox, "2026-10-31 09:00")
    send_all(db, rent, "2026-10-31 09:00")

    mailbox.add(venmo_paid("1,450.00", gmail_id="v-rent"))
    poll(db, rent, mailbox, "2026-11-01 10:00")
    [request] = requests(db)
    assert request.settled_at is not None
    [notice] = send_all(db, rent, "2026-11-01 10:00")
    assert notice.text == "✅ Austin Will paid $1,450.00 for Rent November 2026"


def test_rent_goes_out_even_when_gmail_is_down(db, rent):
    def broken():
        raise ConnectionError("no network")

    bot = make_bot(db, rent, broken, now="2026-10-31 09:05")
    telegram = FakeBot()
    asyncio.run(bot.poll_once(telegram))
    texts = [text for _, text, _ in telegram.sent]
    assert texts[0].startswith("⚠️ Couldn't check Gmail")
    assert texts[1].startswith("🏠 <b>Rent November 2026</b>")
    assert count(db, SplitRequest) == 1
    assert service.outbox(db, rent, at("2026-10-31 09:06")) == []


def test_rent_total_from_env(monkeypatch):
    monkeypatch.setenv("ROOMMATE_NAME", "Austin Will")
    monkeypatch.setenv("ROOMMATE_VENMO", "@AGWill")
    monkeypatch.setenv("RENT_TOTAL", "2,900")
    settings = Settings.from_env()
    assert settings.rent_total_cents == 290000
    assert settings.roommate_venmo == "AGWill"
    monkeypatch.setenv("RENT_TOTAL", "")
    assert Settings.from_env().rent_total_cents is None
