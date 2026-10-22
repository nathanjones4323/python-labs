"""Turn parsed emails into bills, payments and requests of the roommate.

Everything takes the session and `now` as arguments and does no I/O of its
own, so the whole flow runs in tests against a SQLite file. The bot calls
`record` for each new email, then `assume_autopay` and the `due_*` queries,
and marks each thing sent only once Telegram has taken it.

A payment is what triggers a request: the email saying the money went out,
or, for SCE (whose autopay confirmation hasn't been seen yet), the autopay
date passing. Rent is a payment too, made on the last day of each month
(`charge_rent`), so it's asked for, reminded and settled the same way.

When the real email for an assumed payment turns up later it takes the
assumed one's place instead of asking a second time.
"""

from datetime import date, datetime, time, timedelta
from html import escape

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from pay_my_bills.config import Settings
from pay_my_bills.db import (
    ASSUMED,
    ELECTRIC,
    FROM_EMAIL,
    RENT,
    SCHEDULED,
    UTILITY_NAMES,
    Bill,
    Payment,
    ProcessedEmail,
    SplitRequest,
)
from pay_my_bills.parsers import (
    PAID_STATUSES,
    BillReady,
    Email,
    Event,
    PaymentMade,
    RoommatePaid,
    Unrecognized,
)
from pay_my_bills.split import dollars, request_note, share_cents

REMINDER_EVERY = timedelta(days=1)
MAX_REMINDERS = 3
# Two emails about one payment (scheduled, then processed) land this close.
SAME_PAYMENT_WINDOW = timedelta(days=5)
# Rent goes out at 9am local on the last day of the month.
RENT_CHARGE_HOUR = 9
RENT_CATCH_UP = timedelta(days=7)


def gmail_link(gmail_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{gmail_id}"


def seen(session: Session, gmail_ids: list[str]) -> set[str]:
    if not gmail_ids:
        return set()
    rows = session.scalars(
        select(ProcessedEmail.gmail_id).where(ProcessedEmail.gmail_id.in_(gmail_ids))
    )
    return set(rows)


def record(
    session: Session,
    email: Email,
    event: Event | None,
    settings: Settings,
    now: datetime,
) -> ProcessedEmail:
    """Apply one email. Safe to call again with the same email: it's a no-op."""
    existing = session.get(ProcessedEmail, email.gmail_id)
    if existing is not None:
        return existing
    row = ProcessedEmail(
        gmail_id=email.gmail_id,
        kind="ignored",
        sender=email.sender[:320],
        subject=email.subject[:500],
        received_at=email.received_at,
    )
    session.add(row)
    match event:
        case BillReady():
            row.kind = _record_bill(session, email, event)
        case PaymentMade():
            row.kind, row.notice = _record_payment(session, email, event, settings)
        case RoommatePaid():
            row.kind, row.notice = _record_roommate_paid(session, event, settings, now)
        case Unrecognized(reason=reason):
            row.kind = "unrecognized"
            row.notice = (
                f"❓ I found {escape(reason)} and didn't act on it. Please check it:\n"
                f"<b>{escape(email.subject)}</b> from {escape(email.sender)}\n"
                f"{gmail_link(email.gmail_id)}"
            )
    session.flush()
    logger.info(f"Email {email.gmail_id} ({email.subject!r}): {row.kind}")
    return row


def _record_bill(session: Session, email: Email, event: BillReady) -> str:
    duplicate = session.scalar(
        select(Bill).where(
            Bill.utility == event.utility, Bill.statement_date == event.statement_date
        )
    )
    if duplicate is not None:
        return "duplicate_bill"
    session.add(
        Bill(
            utility=event.utility,
            account=event.account,
            statement_date=event.statement_date,
            due_date=event.due_date,
            amount_due_cents=event.amount_due_cents,
            email_id=email.gmail_id,
        )
    )
    return "bill"


def _record_payment(
    session: Session, email: Email, event: PaymentMade, settings: Settings
) -> tuple[str, str | None]:
    name = UTILITY_NAMES[event.utility]
    if event.status not in PAID_STATUSES:
        return "payment_problem", (
            f"⚠️ {name} says a ${dollars(event.amount_cents)} payment is "
            f"<b>{escape(event.status)}</b>. No request was made.\n"
            f"{gmail_link(email.gmail_id)}"
        )
    if _same_payment_seen(session, event):
        return "duplicate_payment", None

    bill = _matching_bill(session, event)
    assumed = bill.payment if bill is not None else None
    if assumed is not None:
        return "payment", _confirm_assumed(assumed, email, event, settings)

    payment = Payment(
        bill=bill,
        utility=event.utility,
        amount_cents=event.amount_cents,
        fee_cents=event.fee_cents,
        total_cents=event.total_cents,
        paid_on=event.paid_on,
        confirmation=event.confirmation,
        source=FROM_EMAIL,
        email_id=email.gmail_id,
    )
    session.add(payment)
    _ask_for_share(session, payment, settings)
    return "payment", None


def _same_payment_seen(session: Session, event: PaymentMade) -> bool:
    if event.confirmation:
        by_code = session.scalar(
            select(Payment.id).where(
                Payment.utility == event.utility,
                Payment.confirmation == event.confirmation,
            )
        )
        if by_code is not None:
            return True
    nearby = session.scalar(
        select(Payment.id).where(
            Payment.utility == event.utility,
            Payment.source == FROM_EMAIL,
            Payment.total_cents == event.total_cents,
            Payment.paid_on >= event.paid_on - SAME_PAYMENT_WINDOW,
            Payment.paid_on <= event.paid_on + SAME_PAYMENT_WINDOW,
        )
    )
    return nearby is not None


def _matching_bill(session: Session, event: PaymentMade) -> Bill | None:
    """The latest bill for this amount that no email has paid yet."""
    bills = session.scalars(
        select(Bill)
        .where(
            Bill.utility == event.utility,
            Bill.amount_due_cents == event.amount_cents,
            Bill.statement_date <= event.paid_on,
        )
        .order_by(Bill.statement_date.desc())
    )
    for bill in bills:
        if bill.payment is None or bill.payment.source == ASSUMED:
            return bill
    return None


def _confirm_assumed(
    payment: Payment, email: Email, event: PaymentMade, settings: Settings
) -> str | None:
    """The confirmation for a payment that was assumed on the autopay date."""
    assumed_total = payment.total_cents
    payment.source = FROM_EMAIL
    payment.email_id = email.gmail_id
    payment.amount_cents = event.amount_cents
    payment.fee_cents = event.fee_cents
    payment.total_cents = event.total_cents
    payment.paid_on = event.paid_on
    payment.confirmation = event.confirmation
    request = payment.request
    if request is None or event.total_cents == assumed_total:
        return None
    share = share_cents(event.total_cents, settings.split_percent)
    name = UTILITY_NAMES[payment.utility]
    if request.requested_at is None and request.is_open:
        # Not asked yet: ask for the right amount instead.
        request.share_cents = share
        request.note = _note(payment)
        request.notified_at = None
        request.reminders = 0
        return None
    return (
        f"⚠️ {name} actually charged ${dollars(event.total_cents)}, not the "
        f"${dollars(assumed_total)} on the bill. You asked "
        f"{escape(settings.roommate_name)} for ${dollars(request.share_cents)}; "
        f"their share is ${dollars(share)}."
    )


def _note(payment: Payment) -> str:
    if payment.utility == RENT:
        return f"Rent {rent_month(payment.paid_on):%B %Y}"
    billed = payment.bill.statement_date if payment.bill else payment.paid_on
    return request_note(UTILITY_NAMES[payment.utility], billed, payment.total_cents)


def _ask_for_share(session: Session, payment: Payment, settings: Settings) -> None:
    session.add(
        SplitRequest(
            payment=payment,
            share_cents=share_cents(payment.total_cents, settings.split_percent),
            note=_note(payment),
        )
    )


def _record_roommate_paid(
    session: Session, event: RoommatePaid, settings: Settings, now: datetime
) -> tuple[str, str | None]:
    if not event.payer.lower().startswith(settings.roommate_name.lower()):
        return "venmo_other", None
    request = session.scalar(
        select(SplitRequest)
        .where(
            SplitRequest.share_cents == event.amount_cents,
            SplitRequest.settled_at.is_(None),
            SplitRequest.skipped_at.is_(None),
        )
        .order_by(SplitRequest.created_at)
    )
    # Anything they pay that isn't an open share (a payment asked for by
    # hand) goes by.
    if request is None:
        return "venmo_other", None
    request.settled_at = now
    return "roommate_paid", (
        f"✅ {escape(event.payer)} paid ${dollars(event.amount_cents)} "
        f"for {escape(request.note)}"
    )


def local_today(settings: Settings, now: datetime) -> date:
    return now.astimezone(settings.timezone).date()


def autopay_date(bill: Bill, settings: Settings) -> date | None:
    if bill.utility != ELECTRIC:
        return None
    return bill.statement_date + timedelta(days=settings.electric_autopay_lag_days)


def assume_autopay(
    session: Session, settings: Settings, now: datetime
) -> list[Payment]:
    """SCE bills whose autopay date has come without a confirmation email:
    take the bill's amount as paid that day, and ask for the share."""
    today = local_today(settings, now)
    made = []
    bills = session.scalars(
        select(Bill).where(Bill.utility == ELECTRIC).order_by(Bill.statement_date)
    )
    for bill in bills:
        when = autopay_date(bill, settings)
        if bill.payment is not None or when is None or when > today:
            continue
        payment = Payment(
            bill=bill,
            utility=bill.utility,
            amount_cents=bill.amount_due_cents,
            total_cents=bill.amount_due_cents,
            paid_on=when,
            source=ASSUMED,
        )
        session.add(payment)
        _ask_for_share(session, payment, settings)
        made.append(payment)
    session.flush()
    return made


def last_day_of_month(day: date) -> date:
    first_of_next = (day.replace(day=1) + timedelta(days=32)).replace(day=1)
    return first_of_next - timedelta(days=1)


def rent_month(charged_on: date) -> date:
    """Rent charged on the last day of a month is for the month after."""
    return charged_on + timedelta(days=1)


def charge_rent(session: Session, settings: Settings, now: datetime) -> list[Payment]:
    """On the last day of the month, from `RENT_CHARGE_HOUR`, ask for the
    roommate's share of next month's rent.

    `paid_on` is the day it's charged, which also makes it once a month. A
    bot that was off that day catches up within `RENT_CATCH_UP`, no later,
    so a fresh install doesn't ask for a month that's long been paid.
    """
    if settings.rent_total_cents is None:
        return []
    local_now = now.astimezone(settings.timezone)
    this_month_end = last_day_of_month(local_now.date())
    last_month_end = local_now.date().replace(day=1) - timedelta(days=1)
    made = []
    for charge_day in (last_month_end, this_month_end):
        due = datetime.combine(
            charge_day, time(RENT_CHARGE_HOUR), tzinfo=settings.timezone
        )
        if not due <= local_now < due + RENT_CATCH_UP:
            continue
        already = session.scalar(
            select(Payment.id).where(
                Payment.utility == RENT, Payment.paid_on == charge_day
            )
        )
        if already is not None:
            continue
        payment = Payment(
            utility=RENT,
            amount_cents=settings.rent_total_cents,
            total_cents=settings.rent_total_cents,
            paid_on=charge_day,
            source=SCHEDULED,
        )
        session.add(payment)
        _ask_for_share(session, payment, settings)
        made.append(payment)
    session.flush()
    return made


def unsent_requests(session: Session) -> list[SplitRequest]:
    return list(
        session.scalars(
            select(SplitRequest)
            .where(
                SplitRequest.notified_at.is_(None),
                SplitRequest.settled_at.is_(None),
                SplitRequest.skipped_at.is_(None),
            )
            .order_by(SplitRequest.id)
        )
    )


def due_reminders(session: Session, now: datetime) -> list[SplitRequest]:
    """Sent, but not yet marked requested, skipped or paid, a day ago or more."""
    return list(
        session.scalars(
            select(SplitRequest)
            .where(
                SplitRequest.notified_at <= now - REMINDER_EVERY,
                SplitRequest.reminders < MAX_REMINDERS,
                SplitRequest.requested_at.is_(None),
                SplitRequest.settled_at.is_(None),
                SplitRequest.skipped_at.is_(None),
            )
            .order_by(SplitRequest.id)
        )
    )


def mark_sent(request: SplitRequest, now: datetime, *, reminder: bool) -> None:
    if reminder:
        request.reminders += 1
    request.notified_at = now


def pending_notices(session: Session) -> list[ProcessedEmail]:
    return list(
        session.scalars(
            select(ProcessedEmail)
            .where(
                ProcessedEmail.notice.is_not(None), ProcessedEmail.notified_at.is_(None)
            )
            .order_by(ProcessedEmail.received_at)
        )
    )


def overdue_bills(session: Session, settings: Settings, now: datetime) -> list[Bill]:
    """Past the due date with no payment seen, and not yet alerted on."""
    today = local_today(settings, now)
    return [
        bill
        for bill in session.scalars(
            select(Bill)
            .where(
                Bill.due_date < today,
                Bill.overdue_alerted_at.is_(None),
            )
            .order_by(Bill.due_date)
        )
        if bill.payment is None
    ]


def open_requests(session: Session) -> list[SplitRequest]:
    return list(
        session.scalars(
            select(SplitRequest)
            .where(SplitRequest.settled_at.is_(None), SplitRequest.skipped_at.is_(None))
            .order_by(SplitRequest.id)
        )
    )


def unpaid_bills(session: Session) -> list[Bill]:
    return [
        bill
        for bill in session.scalars(select(Bill).order_by(Bill.statement_date))
        if bill.payment is None
    ]
