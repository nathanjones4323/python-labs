"""The tables, and the state machine a planned date moves through.

    couples ──── dates ──── restaurants

A `PlannedDate` starts `proposed` (an idea on the table). It becomes
`tentative` once a hold is on the calendar, `booking_pending` once the user
has a link to tap or a reminder to call, `confirmed` when a confirmation
turns up, and `completed` after it happens — or `cancelled` from anywhere
short of done. `PlannedDate.transition` refuses any other move, so a date
can't be confirmed before anyone tried to book it.

Restaurants carry both how they book (`booking_channel` and the slugs a deep
link needs) and when they book (`window_amount` / `window_unit` /
`window_open_time`). Detection results are cached with `channel_checked_at`
so the server doesn't re-read a website on every question.
"""

import enum
import uuid
from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Time,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from date_planner.booking.channels import BookingChannel, WindowUnit
from date_planner.booking.links import LinkTarget
from date_planner.booking.window import BookingWindow
from date_planner.errors import InvalidTransition

DEFAULT_TIMEZONE = "America/Los_Angeles"


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        list[str]: ARRAY(Text),
    }


def _enum(py_enum: type[enum.Enum], name: str) -> Enum:
    # Store the lowercase values, not the member names, so `psql` reads well.
    return Enum(py_enum, name=name, values_callable=lambda e: [m.value for m in e])


class DateStatus(enum.StrEnum):
    PROPOSED = "proposed"  # an idea, nothing on a calendar
    TENTATIVE = "tentative"  # a hold is on the shared calendar
    BOOKING_PENDING = "booking_pending"  # the user has a link or a call to make
    CONFIRMED = "confirmed"  # the restaurant has us down
    COMPLETED = "completed"  # it happened; maybe rated
    CANCELLED = "cancelled"


# Every move a date may make. Terminal states have no entry.
DATE_TRANSITIONS: dict[DateStatus, frozenset[DateStatus]] = {
    DateStatus.PROPOSED: frozenset(
        {DateStatus.TENTATIVE, DateStatus.BOOKING_PENDING, DateStatus.CANCELLED}
    ),
    DateStatus.TENTATIVE: frozenset(
        {DateStatus.BOOKING_PENDING, DateStatus.CONFIRMED, DateStatus.CANCELLED}
    ),
    DateStatus.BOOKING_PENDING: frozenset(
        # Back to tentative when a booking attempt came to nothing and the
        # hold is all that's left.
        {DateStatus.TENTATIVE, DateStatus.CONFIRMED, DateStatus.CANCELLED}
    ),
    DateStatus.CONFIRMED: frozenset({DateStatus.COMPLETED, DateStatus.CANCELLED}),
    DateStatus.COMPLETED: frozenset(),
    DateStatus.CANCELLED: frozenset(),
}


class Couple(Base):
    """Who the dates are for, and what they like.

    One row is enough for now; the table is keyed by couple anyway so a
    second one costs nothing.
    """

    __tablename__ = "couples"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    # Invited as calendar guests — but only once the user says so.
    member_emails: Mapped[list[str]] = mapped_column(default=list)
    shared_calendar_id: Mapped[str | None] = mapped_column(String(320))
    home_area: Mapped[str | None] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default=DEFAULT_TIMEZONE)
    budget_per_date: Mapped[int | None] = mapped_column(Integer)  # USD
    dietary: Mapped[list[str]] = mapped_column(default=list)
    likes: Mapped[list[str]] = mapped_column(default=list)
    dislikes: Mapped[list[str]] = mapped_column(default=list)
    max_travel_minutes: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    dates: Mapped[list["PlannedDate"]] = relationship(back_populates="couple")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class Restaurant(Base):
    """A venue, how it takes reservations, and how far ahead it takes them."""

    __tablename__ = "restaurants"
    __table_args__ = (
        # One row per venue per city, however it was capitalised on the way
        # in. A functional index rather than a plain unique constraint, so
        # "Nobu Malibu" and "nobu malibu" are the same venue.
        Index(
            "ix_restaurants_name_city",
            text("lower(name)"),
            text("lower(city)"),
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    city: Mapped[str] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(20))  # E.164
    website: Mapped[str | None] = mapped_column(String(500))
    timezone: Mapped[str] = mapped_column(String(64), default=DEFAULT_TIMEZONE)

    booking_channel: Mapped[BookingChannel] = mapped_column(
        _enum(BookingChannel, "booking_channel"), default=BookingChannel.UNKNOWN
    )
    platform_slug: Mapped[str | None] = mapped_column(String(200))
    # Resy scopes venues by city, e.g. `los-angeles-ca`.
    platform_city_slug: Mapped[str | None] = mapped_column(String(120))

    window_amount: Mapped[int | None] = mapped_column(SmallInteger)
    window_unit: Mapped[WindowUnit | None] = mapped_column(
        _enum(WindowUnit, "window_unit")
    )
    # Local time the window opens / the phone lines open.
    window_open_time: Mapped[time | None] = mapped_column(Time)

    channel_evidence: Mapped[str | None] = mapped_column(Text)
    channel_confidence: Mapped[float | None]
    channel_checked_at: Mapped[datetime | None]
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def booking_window(self) -> BookingWindow | None:
        """The window as `booking.window` wants it, or None if unrecorded."""
        if self.window_amount is None or self.window_unit is None:
            return None
        return BookingWindow(
            amount=self.window_amount,
            unit=self.window_unit,
            open_time=self.window_open_time,
        )

    @property
    def link_target(self) -> LinkTarget:
        """The venue as `booking.links` wants it."""
        return LinkTarget(
            channel=self.booking_channel,
            timezone=self.tz,
            platform_slug=self.platform_slug,
            platform_city_slug=self.platform_city_slug,
        )


class PlannedDate(Base):
    """One date: the idea, when it is, and how far along the booking got."""

    __tablename__ = "dates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    couple_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("couples.id", ondelete="CASCADE"), index=True
    )
    # Null for a date that isn't a restaurant (a hike, a show).
    restaurant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("restaurants.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(300))
    start_at: Mapped[datetime]
    end_at: Mapped[datetime]
    party_size: Mapped[int] = mapped_column(SmallInteger, default=2)
    status: Mapped[DateStatus] = mapped_column(
        _enum(DateStatus, "date_status"), default=DateStatus.PROPOSED, index=True
    )
    # The `[TENTATIVE]` hold, and (for phone bookings) the "call at 10am"
    # event. Both belong to the Google Calendar connector, not to us.
    calendar_event_id: Mapped[str | None] = mapped_column(String(300))
    reminder_event_id: Mapped[str | None] = mapped_column(String(300))
    booking_url: Mapped[str | None] = mapped_column(Text)
    confirmation_number: Mapped[str | None] = mapped_column(String(120))
    rating: Mapped[int | None] = mapped_column(SmallInteger)  # 1-5, after the date
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    couple: Mapped[Couple] = relationship(back_populates="dates")
    restaurant: Mapped[Restaurant | None] = relationship()

    def transition(self, to: DateStatus) -> None:
        """Move to `to`, or raise if the state machine doesn't allow it."""
        if to == self.status:
            return
        if to not in DATE_TRANSITIONS[self.status]:
            raise InvalidTransition(
                f"Date {self.id} cannot go from {self.status} to {to}"
            )
        self.status = to
