from datetime import date
from zoneinfo import ZoneInfo

from conftest import (
    KUBRA,
    SCE,
    email,
    fixture,
    sce_bill,
    venmo_paid,
    water_bill,
    water_paid,
)
from pay_my_bills.db import ELECTRIC, WATER
from pay_my_bills.gmail import html_to_text
from pay_my_bills.parsers import (
    BillReady,
    PaymentMade,
    RoommatePaid,
    Unrecognized,
    parse,
)

PACIFIC = ZoneInfo("America/Los_Angeles")


def test_water_bill_ready():
    assert parse(water_bill(), PACIFIC) == BillReady(
        utility=WATER,
        account="12345678",
        statement_date=date(2026, 10, 7),
        due_date=date(2026, 10, 27),
        amount_due_cents=5424,
    )


def test_water_bill_dated_by_local_day_not_utc():
    # 8pm Pacific on the 7th is already the 8th in UTC.
    bill = parse(water_bill(received="2026-10-07 20:00"), PACIFIC)
    assert bill.statement_date == date(2026, 10, 7)


def test_water_autopay_confirmation_includes_the_fee():
    assert parse(water_paid(), PACIFIC) == PaymentMade(
        utility=WATER,
        amount_cents=5424,
        fee_cents=295,
        total_cents=5719,
        paid_on=date(2026, 10, 12),
        confirmation="28419413415",
        status="SCHEDULED",
    )


def test_water_failed_payment_status_is_read():
    text = fixture("water_payment_confirmation.txt").replace("SCHEDULED", "FAILED")
    paid = water_paid()
    event = parse(
        email(paid.gmail_id, KUBRA, paid.subject, "2026-10-10 21:33", text), PACIFIC
    )
    assert event.status == "FAILED"


def test_water_one_time_ezpay():
    message = email(
        "ez",
        KUBRA,
        "KUBRA EZ-PAY - Your payment processed successfully",
        "2026-09-20 20:20",
        fixture("water_ezpay.txt"),
    )
    assert parse(message, PACIFIC) == PaymentMade(
        utility=WATER,
        amount_cents=4500,
        fee_cents=295,
        total_cents=4795,
        paid_on=date(2026, 9, 20),
        confirmation="020848",
        status="PAID",
    )


def test_sce_bill_ready():
    assert parse(sce_bill(), PACIFIC) == BillReady(
        utility=ELECTRIC,
        account="00000",
        statement_date=date(2026, 10, 14),
        due_date=date(2026, 11, 2),
        amount_due_cents=8696,
    )


def test_sce_bill_ready_from_html_only():
    text = html_to_text(fixture("sce_bill_ready.html"))
    event = parse(email("e", SCE, "Bill is Ready", "2026-10-14 15:49", text), PACIFIC)
    assert event.amount_due_cents == 8696
    assert event.statement_date == date(2026, 10, 14)
    assert event.due_date == date(2026, 11, 2)


def test_html_to_text_drops_styles_and_splits_cells():
    text = html_to_text(fixture("sce_bill_ready.html"))
    assert "color" not in text
    assert "Amount Due | $86.96" in text


def test_sce_bill_that_doesnt_read_is_flagged_not_dropped():
    event = parse(
        email("e", SCE, "Bill is Ready", "2026-10-14 15:49", "new layout"), PACIFIC
    )
    assert isinstance(event, Unrecognized)


def test_sce_payment_email_is_flagged_until_it_has_a_parser():
    event = parse(
        email("e", SCE, "Thank you for your payment", "2026-10-24 06:00"), PACIFIC
    )
    assert isinstance(event, Unrecognized)


def test_sce_newsletters_are_ignored():
    for subject in (
        "Here's a quick guide on how to read your bill",
        "Your outage experience - less than 2 minute survey",
        "Maximize your savings",
    ):
        assert (
            parse(
                email("e", "donotreply@email.sce.com", subject, "2026-10-01 08:00"),
                PACIFIC,
            )
            is None
        )


def test_venmo_paid_request():
    assert parse(venmo_paid("1,450.00"), PACIFIC) == RoommatePaid(
        payer="Austin Will", amount_cents=145000
    )


def test_venmo_paid_without_a_request():
    message = email(
        "v",
        "venmo@venmo.com",
        "Austin Will paid $51.00 to your Venmo account. Leave it in Venmo or "
        "transfer it to your bank account.",
        "2026-10-11 12:00",
    )
    assert parse(message, PACIFIC) == RoommatePaid(
        payer="Austin Will", amount_cents=5100
    )


def test_venmo_requests_of_me_are_ignored():
    message = email(
        "v", "venmo@venmo.com", "Austin Will requests $67.00", "2026-10-15 10:07"
    )
    assert parse(message, PACIFIC) is None


def test_other_senders_are_ignored():
    assert (
        parse(
            email("x", "friend@example.com", "Bill is Ready", "2026-10-01 08:00"),
            PACIFIC,
        )
        is None
    )
