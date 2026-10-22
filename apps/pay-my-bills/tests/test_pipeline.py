"""The whole flow, from emails in a fake mailbox to what the bot would send."""

from datetime import date, timedelta

from conftest import (
    SCE,
    at,
    email,
    sce_bill,
    venmo_paid,
    water_bill,
    water_paid,
)
from pay_my_bills import service
from pay_my_bills.db import ASSUMED, FROM_EMAIL, Bill, Payment, SplitRequest
from pay_my_bills.parsers import Email, PaymentMade, parse
from pay_my_bills.pipeline import record
from sqlalchemy import func, select


class FakeMailbox:
    def __init__(self, *emails: Email) -> None:
        self.emails = {e.gmail_id: e for e in emails}
        self.fetched: list[str] = []

    def add(self, *emails: Email) -> None:
        self.emails.update({e.gmail_id: e for e in emails})

    def recent_ids(self, days: int) -> list[str]:
        # Gmail lists newest first.
        return sorted(
            self.emails, key=lambda i: self.emails[i].received_at, reverse=True
        )

    def fetch(self, gmail_id: str) -> Email:
        self.fetched.append(gmail_id)
        return self.emails[gmail_id]


def poll(db, settings, mailbox, when: str) -> int:
    return service.poll(db, lambda: mailbox, settings, at(when))


def count(db, model) -> int:
    with db() as session:
        return session.scalar(select(func.count()).select_from(model))


def requests(db) -> list[SplitRequest]:
    with db() as session:
        rows = session.scalars(select(SplitRequest).order_by(SplitRequest.id)).all()
        for row in rows:
            _ = row.payment.bill  # load before the session closes
        return list(rows)


def send_all(db, settings, when: str) -> list[service.Outgoing]:
    items = service.outbox(db, settings, at(when))
    for item in items:
        service.delivered(db, item, at(when))
    return items


def test_water_bill_then_confirmation_asks_once_for_half_the_total(db, settings):
    mailbox = FakeMailbox(water_bill(), water_paid())
    assert poll(db, settings, mailbox, "2026-10-11 08:00") == 2

    [request] = requests(db)
    assert request.share_cents == 2860
    assert request.note == "Ventura Water Oct 2026 ($57.19 total)"
    assert request.payment.bill.amount_due_cents == 5424
    assert request.payment.source == FROM_EMAIL

    [item] = send_all(db, settings, "2026-10-11 08:00")
    assert item.kind == "request"
    assert "Austin Will owes <b>$28.60</b>" in item.text
    assert "$54.24 + $2.95 fee" in item.text
    assert "recipients=AGWill" in item.venmo_url and "amount=28.60" in item.venmo_url


def test_polling_again_does_nothing(db, settings):
    mailbox = FakeMailbox(water_bill(), water_paid())
    poll(db, settings, mailbox, "2026-10-11 08:00")
    send_all(db, settings, "2026-10-11 08:00")
    mailbox.fetched.clear()

    assert poll(db, settings, mailbox, "2026-10-11 08:02") == 0
    assert mailbox.fetched == []
    assert count(db, SplitRequest) == 1
    assert send_all(db, settings, "2026-10-11 08:02") == []


def test_recording_the_same_email_twice_is_a_no_op(db, settings):
    paid = water_paid()
    for _ in range(2):
        with db.begin() as session:
            record(
                session,
                paid,
                parse(paid, settings.timezone),
                settings,
                at("2026-10-11 08:00"),
            )
    assert count(db, Payment) == 1


def test_a_second_email_about_the_same_payment_isnt_a_second_request(db, settings):
    later = email(
        "w-processed",
        water_paid().sender,
        water_paid().subject,
        "2026-10-12 09:00",
        water_paid().text.replace("SCHEDULED", "PROCESSED"),
    )
    poll(
        db, settings, FakeMailbox(water_bill(), water_paid(), later), "2026-10-12 10:00"
    )
    assert count(db, SplitRequest) == 1


def test_confirmation_without_its_bill_still_asks(db, settings):
    poll(db, settings, FakeMailbox(water_paid()), "2026-10-11 08:00")
    [request] = requests(db)
    assert request.payment.bill is None
    assert request.note == "Ventura Water Oct 2026 ($57.19 total)"


def test_failed_payment_is_reported_not_requested(db, settings):
    failed = email(
        "w-failed",
        water_paid().sender,
        water_paid().subject,
        "2026-10-10 21:33",
        water_paid().text.replace("SCHEDULED", "FAILED"),
    )
    poll(db, settings, FakeMailbox(water_bill(), failed), "2026-10-11 08:00")
    assert count(db, SplitRequest) == 0
    items = send_all(db, settings, "2026-10-11 08:00")
    assert [i.kind for i in items] == ["notice"]
    assert "FAILED" in items[0].text


def test_sce_request_waits_for_the_autopay_date(db, settings):
    mailbox = FakeMailbox(sce_bill())
    poll(db, settings, mailbox, "2026-10-23 12:00")
    assert count(db, SplitRequest) == 0

    poll(db, settings, mailbox, "2026-10-24 00:05")
    [request] = requests(db)
    assert request.share_cents == 4348
    assert request.payment.source == ASSUMED
    assert request.payment.paid_on == date(2026, 10, 24)
    [item] = send_all(db, settings, "2026-10-24 00:05")
    assert "no confirmation email yet" in item.text

    # Only once.
    poll(db, settings, mailbox, "2026-10-25 00:05")
    assert count(db, SplitRequest) == 1


def _sce_confirmation(total_cents: int) -> tuple[Email, PaymentMade]:
    message = email("e-paid", SCE, "Payment received", "2026-10-24 06:00")
    event = PaymentMade(
        utility="electric",
        amount_cents=total_cents,
        fee_cents=0,
        total_cents=total_cents,
        paid_on=date(2026, 10, 24),
        confirmation="SCE1",
        status="PAID",
    )
    return message, event


def test_sce_confirmation_after_assumed_payment_replaces_it(db, settings):
    poll(db, settings, FakeMailbox(sce_bill()), "2026-10-24 00:05")
    send_all(db, settings, "2026-10-24 00:05")

    message, event = _sce_confirmation(8696)
    with db.begin() as session:
        record(session, message, event, settings, at("2026-10-24 07:00"))

    [request] = requests(db)
    assert request.payment.source == FROM_EMAIL
    assert request.payment.confirmation == "SCE1"
    assert count(db, Payment) == 1
    assert send_all(db, settings, "2026-10-24 07:00") == []


def test_sce_confirmation_for_a_different_amount_fixes_an_unrequested_share(
    db, settings
):
    poll(db, settings, FakeMailbox(sce_bill()), "2026-10-24 00:05")
    send_all(db, settings, "2026-10-24 00:05")

    # The bill was $86.96 but $90.00 left the account. Not requested yet, so
    # the request is corrected and sent again.
    message, event = _sce_confirmation(8696)
    event = PaymentMade(**{**event.__dict__, "total_cents": 9000, "fee_cents": 304})
    with db.begin() as session:
        record(session, message, event, settings, at("2026-10-24 07:00"))

    [request] = requests(db)
    assert request.share_cents == 4500
    [item] = send_all(db, settings, "2026-10-24 07:00")
    assert item.kind == "request" and "$45.00" in item.text


def test_sce_confirmation_for_a_different_amount_after_requesting_warns(db, settings):
    poll(db, settings, FakeMailbox(sce_bill()), "2026-10-24 00:05")
    [item] = send_all(db, settings, "2026-10-24 00:05")
    service.mark(db, item.ref, "done", at("2026-10-24 00:10"))

    message, event = _sce_confirmation(8696)
    event = PaymentMade(**{**event.__dict__, "total_cents": 9000, "fee_cents": 304})
    with db.begin() as session:
        record(session, message, event, settings, at("2026-10-24 07:00"))

    [request] = requests(db)
    assert request.share_cents == 4348
    [notice] = send_all(db, settings, "2026-10-24 07:00")
    assert notice.kind == "notice"
    assert "$90.00" in notice.text and "$45.00" in notice.text


def test_roommate_paying_settles_the_request(db, settings):
    mailbox = FakeMailbox(water_bill(), water_paid())
    poll(db, settings, mailbox, "2026-10-11 08:00")
    send_all(db, settings, "2026-10-11 08:00")

    mailbox.add(venmo_paid("28.60", gmail_id="v1"))
    mailbox.add(venmo_paid("1,450.00", gmail_id="v-rent"))
    poll(db, settings, mailbox, "2026-10-15 10:00")

    [request] = requests(db)
    assert request.settled_at is not None
    [notice] = send_all(db, settings, "2026-10-15 10:00")
    assert "✅ Austin Will paid $28.60" in notice.text


def test_someone_else_paying_the_same_amount_doesnt_settle(db, settings):
    mailbox = FakeMailbox(water_bill(), water_paid(), venmo_paid("28.60", payer="Erik"))
    poll(db, settings, mailbox, "2026-10-15 10:00")
    [request] = requests(db)
    assert request.settled_at is None


def test_unmarked_requests_are_reminded_daily_three_times(db, settings):
    poll(db, settings, FakeMailbox(water_bill(), water_paid()), "2026-10-11 08:00")
    send_all(db, settings, "2026-10-11 08:00")
    assert send_all(db, settings, "2026-10-11 20:00") == []

    kinds = []
    for day in range(1, 6):
        now = at("2026-10-11 08:00") + timedelta(days=day)
        items = service.outbox(db, settings, now)
        for item in items:
            service.delivered(db, item, now)
        kinds.extend(i.kind for i in items)
    assert kinds == ["reminder"] * 3


def test_marked_requested_stops_reminders(db, settings):
    poll(db, settings, FakeMailbox(water_bill(), water_paid()), "2026-10-11 08:00")
    [item] = send_all(db, settings, "2026-10-11 08:00")
    assert service.mark(db, item.ref, "done", at("2026-10-11 08:01")).startswith("📨")
    assert send_all(db, settings, "2026-10-13 08:00") == []


def test_skip_stops_everything(db, settings):
    poll(db, settings, FakeMailbox(water_bill(), water_paid()), "2026-10-11 08:00")
    [item] = send_all(db, settings, "2026-10-11 08:00")
    assert service.mark(db, item.ref, "skip", at("2026-10-11 08:01")) == "⏭ skipped"
    assert send_all(db, settings, "2026-10-13 08:00") == []
    assert "Nothing open" in service.status(db, settings)


def test_marked_paid_by_hand_settles_it(db, settings):
    poll(db, settings, FakeMailbox(water_bill(), water_paid()), "2026-10-11 08:00")
    [item] = send_all(db, settings, "2026-10-11 08:00")
    assert service.mark(db, item.ref, "paid", at("2026-10-11 08:01")) == "✅ paid"
    assert send_all(db, settings, "2026-10-13 08:00") == []
    assert "Nothing open" in service.status(db, settings)


def test_unpaid_bill_past_due_is_alerted_once(db, settings):
    poll(db, settings, FakeMailbox(water_bill()), "2026-10-27 12:00")
    assert send_all(db, settings, "2026-10-27 12:00") == []
    [alert] = send_all(db, settings, "2026-10-28 08:00")
    assert alert.kind == "overdue" and "$54.24" in alert.text
    assert send_all(db, settings, "2026-10-29 08:00") == []


def test_unread_sce_payment_email_is_passed_on(db, settings):
    odd = email("e-odd", SCE, "Your AutoPay payment is scheduled", "2026-10-23 06:00")
    poll(db, settings, FakeMailbox(odd), "2026-10-23 07:00")
    [notice] = send_all(db, settings, "2026-10-23 07:00")
    assert "Your AutoPay payment is scheduled" in notice.text
    assert "mail.google.com" in notice.text


def test_status_lists_open_requests_and_waiting_bills(db, settings):
    poll(
        db,
        settings,
        FakeMailbox(water_bill(), water_paid(), sce_bill()),
        "2026-10-15 08:00",
    )
    text = service.status(db, settings)
    assert "Ventura Water Oct 2026 ($57.19 total): $28.60, 🕓" in text
    assert "SCE Oct 2026: $86.96, autopay Oct 24" in text


def test_duplicate_bill_email_is_one_bill(db, settings):
    poll(
        db,
        settings,
        FakeMailbox(
            sce_bill(), sce_bill(gmail_id="e-bill-2", received="2026-10-14 16:00")
        ),
        "2026-10-15 08:00",
    )
    assert count(db, Bill) == 1
