"""What the bot says, as Telegram HTML. Pure functions over the models."""

from datetime import date
from html import escape

from pay_my_bills.config import Settings
from pay_my_bills.db import (
    ASSUMED,
    ELECTRIC,
    RENT,
    UTILITY_NAMES,
    Bill,
    SplitRequest,
)
from pay_my_bills.pipeline import autopay_date, billed_on, rent_month
from pay_my_bills.split import dollars, venmo_request_url

ICONS = {ELECTRIC: "⚡", RENT: "🏠"}
WATER_ICON = "💧"


def _icon(utility: str) -> str:
    return ICONS.get(utility, WATER_ICON)


def _day(value: date) -> str:
    return f"{value:%b} {value.day}"


def venmo_url(request: SplitRequest, settings: Settings) -> str:
    return venmo_request_url(settings.roommate_venmo, request.share_cents, request.note)


def request_text(request: SplitRequest, settings: Settings, *, reminder: bool) -> str:
    payment = request.payment
    name = UTILITY_NAMES[payment.utility]
    billed = billed_on(payment)
    if payment.utility == RENT:
        # The note names the month the rent is for: "Rent November 2026".
        headline = f"<b>{escape(request.note)}</b>: ${dollars(payment.total_cents)}"
    else:
        if payment.source == ASSUMED:
            how = (
                f"autopay expected {_day(payment.paid_on)} "
                "(SCE doesn't email when it's taken, so this is the bill's "
                "amount and date: check your bank)"
            )
        else:
            how = f"paid {_day(payment.paid_on)}"
        headline = (
            f"<b>{name}</b> {billed:%b %Y}: ${dollars(payment.total_cents)}, {how}"
        )
    lines = [
        "⏰ <i>Reminder</i>" if reminder else None,
        f"{_icon(payment.utility)} {headline}",
        f"${dollars(payment.amount_cents)} + ${dollars(payment.fee_cents)} fee"
        if payment.fee_cents
        else None,
        f"{escape(settings.roommate_name)} owes <b>${dollars(request.share_cents)}</b> "
        f"({settings.split_percent}%)",
        f"Or by Zelle to {escape(settings.roommate_zelle)}"
        if settings.roommate_zelle
        else None,
    ]
    return "\n".join(line for line in lines if line)


def short_label(request: SplitRequest) -> str:
    """A request in a few characters, for a button: "💧 Oct $28.60"."""
    payment = request.payment
    if payment.utility == RENT:
        month = rent_month(payment.paid_on)
    else:
        month = billed_on(payment)
    return f"{_icon(payment.utility)} {month:%b} ${dollars(request.share_cents)}"


def overdue_text(bill: Bill) -> str:
    name = UTILITY_NAMES[bill.utility]
    return (
        f"⚠️ <b>{name}</b> bill of ${dollars(bill.amount_due_cents)} was due "
        f"{_day(bill.due_date)} and I haven't seen it paid. Is autopay still on?"
    )


def request_state(request: SplitRequest) -> str:
    if request.settled_at:
        return "✅ paid"
    if request.skipped_at:
        return "⏭ skipped"
    if request.requested_at:
        return "📨 requested, waiting on payment"
    if request.notified_at:
        return "🔔 not requested yet"
    return "🕓 about to be sent"


def status_text(
    requests: list[SplitRequest], bills: list[Bill], settings: Settings
) -> str:
    lines = []
    if requests:
        lines.append(f"<b>Open with {escape(settings.roommate_name)}</b>")
        for request in requests:
            lines.append(
                f"• {escape(request.note)}: ${dollars(request.share_cents)}, "
                f"{request_state(request)}"
            )
    if bills:
        lines.append("<b>Bills waiting on payment</b>")
        for bill in bills:
            when = autopay_date(bill, settings)
            expected = (
                f"autopay expected {_day(when)}"
                if when
                else f"due {_day(bill.due_date)}"
                if bill.due_date
                else "no due date"
            )
            lines.append(
                f"• {_icon(bill.utility)} {UTILITY_NAMES[bill.utility]} "
                f"{bill.statement_date:%b %Y}: "
                f"${dollars(bill.amount_due_cents)}, {expected}"
            )
    return "\n".join(lines) or "Nothing open: every bill is paid and split. 🎉"
