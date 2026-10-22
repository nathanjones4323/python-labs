"""Read the utilities' and Venmo's emails into events.

Pure functions over an `Email` (sender, subject, plain-text body), so every
format is tested against a real message (tests/fixtures). The body is
whatever text Gmail gives: KUBRA's arrives as `*Payment amount: $* 54.24`,
SCE's as a table, `| Amount Due | $86.96 |`, so the patterns skip any run of
`|`, `*`, `:`, `$` and whitespace between a label and its value.

`parse` returns None for mail that isn't about a bill (SCE's surveys and
newsletters). When a message is from a biller and looks like a bill or a
payment but doesn't read, it returns `Unrecognized`, so a person sees it and
the format can be added, rather than a payment slipping by unnoticed.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from pay_my_bills.db.models import ELECTRIC, WATER

# KUBRA reports a scheduled autopay as SCHEDULED, the day before the debit.
PAID_STATUSES = {"SCHEDULED", "PAID", "PROCESSED", "COMPLETED", "SUCCESSFUL"}

_GAP = r"[\s|*:$#]*"
_MONEY = r"([\d,]+\.\d{2})"
_SLASH_DATE = r"(\d{1,2}/\d{1,2}/\d{4})"


@dataclass(frozen=True)
class Email:
    gmail_id: str
    sender: str
    subject: str
    received_at: datetime
    text: str


@dataclass(frozen=True)
class BillReady:
    utility: str
    account: str | None
    statement_date: date
    due_date: date | None
    amount_due_cents: int


@dataclass(frozen=True)
class PaymentMade:
    utility: str
    amount_cents: int
    fee_cents: int
    total_cents: int
    paid_on: date
    confirmation: str | None
    status: str


@dataclass(frozen=True)
class RoommatePaid:
    payer: str
    amount_cents: int


@dataclass(frozen=True)
class Unrecognized:
    reason: str


Event = BillReady | PaymentMade | RoommatePaid | Unrecognized


class _Missing(Exception):
    pass


def cents(text: str) -> int:
    return int(Decimal(text.replace(",", "")) * 100)


def _field(text: str, label: str, value: str) -> str:
    match = re.search(label + _GAP + value, text, re.IGNORECASE)
    if match is None:
        raise _Missing(label.replace("\\", ""))
    return match.group(1)


def _optional_field(text: str, label: str, value: str) -> str | None:
    try:
        return _field(text, label, value)
    except _Missing:
        return None


def _slash_date(text: str) -> date:
    return datetime.strptime(text, "%m/%d/%Y").date()


def _sender_domain(sender: str) -> str:
    address = sender.rsplit("<", 1)[-1].rstrip(">").strip().lower()
    return address.rsplit("@", 1)[-1]


def parse(email: Email, tz: ZoneInfo) -> Event | None:
    domain = _sender_domain(email.sender)
    subject = email.subject.lower()
    try:
        if domain == "kubra.com":
            return _kubra(email, subject, tz)
        if domain == "sce.com" or domain.endswith(".sce.com"):
            return _sce(email, subject)
        if domain == "venmo.com":
            return _venmo(email)
    except (_Missing, ValueError) as exc:
        return Unrecognized(f"couldn't read {exc}")
    return None


def _kubra(email: Email, subject: str, tz: ZoneInfo) -> Event | None:
    text = email.text
    if "ventura water ebill" in subject:
        return BillReady(
            utility=WATER,
            account=_optional_field(text, r"Account number", r"(\d+)"),
            # The email doesn't print a statement date; it's sent the day the
            # bill is generated.
            statement_date=email.received_at.astimezone(tz).date(),
            due_date=_slash_date(_field(text, r"Due date", _SLASH_DATE)),
            amount_due_cents=cents(_field(text, r"Amount due", _MONEY)),
        )
    if "ventura water" in subject and "payment confirmation" in subject:
        amount = cents(_field(text, r"Payment amount", _MONEY))
        fee = cents(_optional_field(text, r"Service fee amount", _MONEY) or "0.00")
        total = _optional_field(text, r"Total amount", _MONEY)
        return PaymentMade(
            utility=WATER,
            amount_cents=amount,
            fee_cents=fee,
            total_cents=cents(total) if total else amount + fee,
            paid_on=_slash_date(_field(text, r"Payment date", _SLASH_DATE)),
            confirmation=_optional_field(text, r"Confirmation code", r"(\w+)"),
            status=_kubra_status(text),
        )
    if "ez-pay" in subject and "city of ventura" in text.lower():
        if "success" not in subject:
            return Unrecognized("a KUBRA EZ-PAY email that doesn't say it succeeded")
        amount = cents(_field(text, r"Payment Amount", _MONEY))
        fee = cents(_optional_field(text, r"Service Fee", _MONEY) or "0.00")
        total = _optional_field(text, r"Total paid", _MONEY)
        paid_on = _field(text, r"Paid on", r"([A-Z][a-z]{2} \d{1,2}, \d{4})")
        return PaymentMade(
            utility=WATER,
            amount_cents=amount,
            fee_cents=fee,
            total_cents=cents(total) if total else amount + fee,
            paid_on=datetime.strptime(paid_on, "%b %d, %Y").date(),
            confirmation=_optional_field(text, r"Confirmation #", r"(\w+)"),
            status=_optional_field(text, r"Status", r"([A-Z]+)\b") or "PAID",
        )
    if "ventura" in subject or "payment" in subject:
        return Unrecognized("a Ventura Water email in a format this app doesn't know")
    return None


def _kubra_status(text: str) -> str:
    # "The payment for Account Number: 12345678 is *SCHEDULED*"
    match = re.search(r"\bis\s*\*?([A-Z]{4,})\b", text)
    if match is None:
        raise _Missing("payment status")
    return match.group(1)


def _sce(email: Email, subject: str) -> Event | None:
    text = email.text
    if "bill is ready" in subject:
        return BillReady(
            utility=ELECTRIC,
            account=_optional_field(text, r"Customer Account", r"X*(\d+)"),
            statement_date=_slash_date(_field(text, r"Statement Date", _SLASH_DATE)),
            due_date=_slash_date(_field(text, r"Due Date", _SLASH_DATE)),
            amount_due_cents=cents(_field(text, r"Amount Due", _MONEY)),
        )
    # No SCE autopay confirmation has been seen yet. Until one has and gets
    # its own parser, anything payment-shaped goes to the person to read.
    # ("bill" alone would catch the "how to read your bill" newsletters.)
    if re.search(r"payment|paid|auto ?pay|receipt|deduct|thank you", subject):
        return Unrecognized("an SCE payment email this app can't read yet")
    return None


# "Austin Will paid your $28.60 request", or, when they pay without being
# asked, "Austin Will paid $28.60 to your Venmo account. Leave it in ...".
_VENMO_PAID = re.compile(
    r"(.+?) paid (?:your \$" + _MONEY + r" request|\$" + _MONEY + r" to your Venmo)"
)


def _venmo(email: Email) -> Event | None:
    match = _VENMO_PAID.match(email.subject)
    if match is None:
        return None
    amount = match.group(2) or match.group(3)
    return RoommatePaid(payer=match.group(1).strip(), amount_cents=cents(amount))
