"""The tables: every email seen, the bills they announce, the payments that
settle them, and the share asked of the roommate for each payment.

Money is whole cents in integers; SQLite has no exact decimal type and a
float would turn $28.60 into $28.599999.
"""

from datetime import UTC, date, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

WATER = "water"
ELECTRIC = "electric"
# Not a utility, but charged to the roommate the same way (see
# `pipeline.charge_rent`).
RENT = "rent"
UTILITY_NAMES = {WATER: "Ventura Water", ELECTRIC: "SCE", RENT: "Rent"}

# Payment.source
FROM_EMAIL = "email"
# No confirmation email (yet): assumed paid on the autopay date.
ASSUMED = "assumed"
# Charged on a schedule rather than prompted by an email: rent.
SCHEDULED = "scheduled"


class UTCDateTime(TypeDecorator):
    """A timezone-aware datetime, stored as naive UTC.

    SQLite drops the offset of a `DateTime(timezone=True)`, so what comes back
    would be naive and fail every comparison with an aware `now`.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"Naive datetime {value!r}; pass an aware one")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ProcessedEmail(Base):
    """Every message the poll has looked at, so none is handled twice.

    Ones a person should hear about (an email that looks like a payment but
    didn't parse, the roommate paying) carry the message in `notice` until
    the bot has sent it.
    """

    __tablename__ = "processed_emails"

    gmail_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    sender: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(500))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime())
    notice: Mapped[str | None] = mapped_column(Text)
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    processed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Bill(Base):
    __tablename__ = "bills"
    __table_args__ = (UniqueConstraint("utility", "statement_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    utility: Mapped[str] = mapped_column(String(20))
    account: Mapped[str | None] = mapped_column(String(64))
    statement_date: Mapped[date]
    due_date: Mapped[date | None]
    amount_due_cents: Mapped[int]
    email_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    # Set once the "no payment by the due date" alert has gone out.
    overdue_alerted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    payment: Mapped["Payment | None"] = relationship(back_populates="bill")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bill_id: Mapped[int | None] = mapped_column(
        ForeignKey("bills.id", ondelete="SET NULL"), unique=True
    )
    utility: Mapped[str] = mapped_column(String(20))
    amount_cents: Mapped[int]
    fee_cents: Mapped[int] = mapped_column(default=0)
    # What actually left the bank account: amount plus fee. The split is of this.
    total_cents: Mapped[int]
    paid_on: Mapped[date]
    confirmation: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16))
    email_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    bill: Mapped[Bill | None] = relationship(back_populates="payment")
    request: Mapped["SplitRequest | None"] = relationship(back_populates="payment")


class SplitRequest(Base):
    """The roommate's share of one payment, and how far asking for it got.

    The app never sends the Venmo request itself: it hands the person a
    prefilled link and they press Request in Venmo. `requested_at` is them
    saying they did; `settled_at` is Venmo's "paid your request" email.
    """

    __tablename__ = "split_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payment_id: Mapped[int] = mapped_column(
        ForeignKey("payments.id", ondelete="CASCADE"), unique=True
    )
    share_cents: Mapped[int]
    note: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    reminders: Mapped[int] = mapped_column(default=0)
    requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    skipped_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    settled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    payment: Mapped[Payment] = relationship(back_populates="request")

    @property
    def is_open(self) -> bool:
        """Still waiting on the roommate's money."""
        return self.settled_at is None and self.skipped_at is None
