"""The tables, and how a date's status follows from its stops.

    couples ──── dates ──── date_stops ──── venues
                                              │
                                        travel_cache

A `PlannedDate` is a day: a title, a window, and how far apart its stops may
be (`max_leg_minutes`). Everything bookable lives on its ordered
`DateStop`s — a hike, a timed museum entry, a sunset dinner — each with its
own times, booking state and calendar event.

The date's `status` is *derived* from its stops (`derive_status`) and stored
after every stop change, so a query can filter on it: any stop still waiting
on a booking makes the date `booking_pending`; every stop booked or not
needing it, with times set, makes it `confirmed`; anything else is
`planning`. `completed` and `cancelled` are set by hand and stick.

Venues carry both how they book (`booking_channel` and the slugs a deep link
needs) and when they book (`window_amount` / `window_unit` /
`window_open_time`), plus where they are (`lat` / `lng`) so legs between
stops can be timed. Detection results are cached with `channel_checked_at`,
travel times in `travel_cache`.
"""

import enum
import uuid
from collections.abc import Iterable
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from date_planner.booking.channels import BookingChannel, WindowUnit
from date_planner.booking.links import LinkTarget
from date_planner.booking.window import BookingWindow

DEFAULT_TIMEZONE = "America/Los_Angeles"


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        list[str]: ARRAY(Text),
    }


def _enum(py_enum: type[enum.Enum], name: str) -> Enum:
    # Store the lowercase values, not the member names, so `psql` reads well.
    return Enum(py_enum, name=name, values_callable=lambda e: [m.value for m in e])


class VenueType(enum.StrEnum):
    RESTAURANT = "restaurant"
    TRAIL = "trail"
    MUSEUM = "museum"
    BAR = "bar"
    PARK = "park"
    VIEWPOINT = "viewpoint"
    OTHER = "other"


class DateStatus(enum.StrEnum):
    PLANNING = "planning"  # stops still being chosen or timed
    BOOKING_PENDING = "booking_pending"  # a stop is waiting on a booking
    CONFIRMED = "confirmed"  # every stop booked or needing none, all timed
    COMPLETED = "completed"  # it happened; maybe rated (set by hand)
    CANCELLED = "cancelled"  # (set by hand)


# Set by hand and never overwritten by `derive_status`.
STICKY_STATUSES = frozenset({DateStatus.COMPLETED, DateStatus.CANCELLED})


class StopKind(enum.StrEnum):
    ACTIVITY = "activity"
    MEAL = "meal"
    TICKETED = "ticketed"


class Anchor(enum.StrEnum):
    NONE = "none"  # flexible: `plan_timeline` places it
    FIXED = "fixed"  # time set by a reservation or timed entry
    SUNSET = "sunset"  # sunset + `sunset_offset_min`


class ReservationNeed(enum.StrEnum):
    REQUIRED = "required"
    RECOMMENDED = "recommended"
    NONE = "none"
    UNKNOWN = "unknown"


class StopBookingStatus(enum.StrEnum):
    NOT_NEEDED = "not_needed"
    NEEDED = "needed"
    LINK_SENT = "link_sent"
    REMINDER_SET = "reminder_set"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"  # dropped from the day; ignored by the status


# A stop in one of these is still waiting on somebody to book it.
PENDING_BOOKING = frozenset(
    {
        StopBookingStatus.NEEDED,
        StopBookingStatus.LINK_SENT,
        StopBookingStatus.REMINDER_SET,
    }
)
SETTLED_BOOKING = frozenset({StopBookingStatus.NOT_NEEDED, StopBookingStatus.CONFIRMED})


class TravelMode(enum.StrEnum):
    DRIVING = "driving"
    WALKING = "walking"


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


class Venue(Base):
    """A place a stop happens: how it books, how far ahead, and where it is."""

    __tablename__ = "venues"
    __table_args__ = (
        # One row per venue per city, however it was capitalised on the way
        # in. A functional index rather than a plain unique constraint, so
        # "Nobu Malibu" and "nobu malibu" are the same venue.
        Index(
            "ix_venues_name_city",
            text("lower(name)"),
            text("lower(city)"),
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    city: Mapped[str] = mapped_column(String(120))
    venue_type: Mapped[VenueType] = mapped_column(_enum(VenueType, "venue_type"))
    address: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(20))  # E.164
    website: Mapped[str | None] = mapped_column(String(500))
    timezone: Mapped[str] = mapped_column(String(64), default=DEFAULT_TIMEZONE)
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    # A page about the place that isn't its own site, e.g. on AllTrails.
    external_url: Mapped[str | None] = mapped_column(Text)
    default_duration_min: Mapped[int | None] = mapped_column(Integer)

    booking_channel: Mapped[BookingChannel] = mapped_column(
        _enum(BookingChannel, "booking_channel"), default=BookingChannel.UNKNOWN
    )
    # The platform's venue slug; for `ticketed`, the venue's booking page URL.
    platform_slug: Mapped[str | None] = mapped_column(String(200))
    # Resy scopes venues by city, e.g. `los-angeles-ca`.
    platform_city_slug: Mapped[str | None] = mapped_column(String(120))
    # Google's id for the place; the Resy connector's widget takes nothing
    # else. Set by `find_google_place`.
    google_place_id: Mapped[str | None] = mapped_column(Text)

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
    def has_coordinates(self) -> bool:
        return self.lat is not None and self.lng is not None

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
    """One date: a day, its window, and the stops in order."""

    __tablename__ = "dates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    couple_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("couples.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300))
    day: Mapped[date]
    # The agent's "nearby" for this date: no leg between stops longer.
    max_leg_minutes: Mapped[int] = mapped_column(SmallInteger, default=25)
    # The whole day's window, local to the couple, if the user gave one.
    start_after: Mapped[time | None] = mapped_column(Time)
    end_before: Mapped[time | None] = mapped_column(Time)
    status: Mapped[DateStatus] = mapped_column(
        _enum(DateStatus, "date_status"), default=DateStatus.PLANNING, index=True
    )
    rating: Mapped[int | None] = mapped_column(SmallInteger)  # 1-5, after the date
    notes: Mapped[str | None] = mapped_column(Text)
    # The Artifact the preview page was published to, to republish in place.
    preview_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    couple: Mapped[Couple] = relationship(back_populates="dates")
    # selectin: the tools always want the stops, and an async session can't
    # lazy-load them on attribute access.
    stops: Mapped[list["DateStop"]] = relationship(
        back_populates="date",
        order_by="DateStop.position",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class DateStop(Base):
    """One thing in the day: where, when, and how far its booking got."""

    __tablename__ = "date_stops"
    __table_args__ = (
        # Deferred, so `reorder_stops` can swap two positions in one
        # transaction without tripping over itself mid-way.
        UniqueConstraint(
            "date_id",
            "position",
            name="uq_date_stops_date_position",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    date_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dates.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(SmallInteger)
    title: Mapped[str] = mapped_column(String(300))
    kind: Mapped[StopKind] = mapped_column(_enum(StopKind, "stop_kind"))
    # Null while the stop is an open slot ("a sunset dinner somewhere").
    venue_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("venues.id", ondelete="SET NULL")
    )
    party_size: Mapped[int] = mapped_column(SmallInteger, default=2)
    start_at: Mapped[datetime | None]
    end_at: Mapped[datetime | None]
    est_duration_min: Mapped[int | None] = mapped_column(Integer)
    anchor: Mapped[Anchor] = mapped_column(_enum(Anchor, "stop_anchor"))
    sunset_offset_min: Mapped[int] = mapped_column(SmallInteger, default=-30)
    reservation_need: Mapped[ReservationNeed] = mapped_column(
        _enum(ReservationNeed, "reservation_need")
    )
    booking_status: Mapped[StopBookingStatus] = mapped_column(
        _enum(StopBookingStatus, "stop_booking_status")
    )
    booking_url: Mapped[str | None] = mapped_column(Text)
    confirmation_number: Mapped[str | None] = mapped_column(String(120))
    # This stop's event on the shared calendar, and (for phone bookings) the
    # "call at 10am" reminder. Both belong to the Google Calendar connector.
    calendar_event_id: Mapped[str | None] = mapped_column(String(300))
    reminder_event_id: Mapped[str | None] = mapped_column(String(300))
    # Written by `plan_timeline`: the leg from the previous stop to this one.
    travel_min_from_prev: Mapped[int | None] = mapped_column(SmallInteger)
    travel_mode: Mapped[TravelMode] = mapped_column(
        _enum(TravelMode, "travel_mode"), default=TravelMode.DRIVING
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    date: Mapped[PlannedDate] = relationship(back_populates="stops")
    venue: Mapped[Venue | None] = relationship(lazy="selectin")

    @property
    def venue_name(self) -> str | None:
        return self.venue.name if self.venue is not None else None


class TravelCache(Base):
    """A leg between two venues as one provider timed it, unpadded.

    Directional: A→B and B→A are separate rows, since one-way streets and
    freeway ramps make them differ.
    """

    __tablename__ = "travel_cache"

    from_venue_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("venues.id", ondelete="CASCADE"), primary_key=True
    )
    to_venue_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("venues.id", ondelete="CASCADE"), primary_key=True
    )
    mode: Mapped[TravelMode] = mapped_column(
        _enum(TravelMode, "travel_mode"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    minutes: Mapped[float]
    meters: Mapped[float]
    computed_at: Mapped[datetime] = mapped_column(server_default=func.now())


def derive_status(current: DateStatus, stops: Iterable[DateStop]) -> DateStatus:
    """The status a date's stops add up to; see the module docstring.

    Cancelled stops are out of the day, so they count for nothing. A date
    with no stops yet is still `planning`.
    """
    if current in STICKY_STATUSES:
        return current
    live = [s for s in stops if s.booking_status is not StopBookingStatus.CANCELLED]
    if any(s.booking_status in PENDING_BOOKING for s in live):
        return DateStatus.BOOKING_PENDING
    if live and all(
        s.booking_status in SETTLED_BOOKING
        and s.start_at is not None
        and s.end_at is not None
        for s in live
    ):
        return DateStatus.CONFIRMED
    return DateStatus.PLANNING
