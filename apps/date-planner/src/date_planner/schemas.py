"""What the tools take and hand back.

These are the MCP tool signatures, so they double as the schema an agent
reads. Output models are built straight off the ORM rows
(`from_attributes`); input models are permissive about what they leave out,
because the skill fills a profile in over several turns.
"""

import uuid
from datetime import datetime, time
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from date_planner.booking.channels import (
    BookingChannel,
    WindowState,
    WindowUnit,
)
from date_planner.db.models import DateStatus

PartySize = Annotated[int, Field(ge=1, le=20)]
Rating = Annotated[int, Field(ge=1, le=5)]


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- couples ---------------------------------------------------------------


class CoupleProfile(Out):
    """Everything the skill needs to pick an idea and hold a slot."""

    id: uuid.UUID
    name: str
    member_emails: list[str]
    shared_calendar_id: str | None
    home_area: str | None
    timezone: str
    budget_per_date: int | None
    dietary: list[str]
    likes: list[str]
    dislikes: list[str]
    max_travel_minutes: int | None


class CoupleProfileIn(BaseModel):
    """A profile to insert, or — with `id` — one to update.

    Only the fields present are written, so a correction to one preference
    doesn't wipe the rest.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    name: str | None = None
    member_emails: list[str] | None = None
    shared_calendar_id: str | None = None
    home_area: str | None = None
    timezone: str | None = None
    budget_per_date: int | None = None
    dietary: list[str] | None = None
    likes: list[str] | None = None
    dislikes: list[str] | None = None
    max_travel_minutes: int | None = None


# --- restaurants -----------------------------------------------------------


class RestaurantOut(Out):
    id: uuid.UUID
    name: str
    city: str
    address: str | None
    phone: str | None
    website: str | None
    timezone: str
    booking_channel: BookingChannel
    platform_slug: str | None
    platform_city_slug: str | None
    window_amount: int | None
    window_unit: WindowUnit | None
    window_open_time: time | None
    channel_evidence: str | None
    channel_confidence: float | None
    channel_checked_at: datetime | None
    notes: str | None


class RestaurantIn(BaseModel):
    """A venue to insert, or — with `id` — one to update.

    Without an `id`, `name` and `city` identify the row: a second upsert of
    "Nobu Malibu" in "Malibu" updates the first one rather than duplicating
    it.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    name: str | None = None
    city: str | None = None
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    timezone: str | None = None
    booking_channel: BookingChannel | None = None
    platform_slug: str | None = None
    platform_city_slug: str | None = None
    window_amount: int | None = None
    window_unit: WindowUnit | None = None
    window_open_time: time | None = None
    notes: str | None = None


class DetectionOut(BaseModel):
    """What reading the restaurant's website said about how it books."""

    restaurant_id: uuid.UUID | None = None
    channel: BookingChannel
    platform_slug: str | None = None
    platform_city_slug: str | None = None
    phone: str | None = None
    confidence: float
    evidence: str
    window_amount: int | None = None
    window_unit: WindowUnit | None = None
    window_open_time: time | None = None
    # True when this came from `channel_checked_at`, not a fresh fetch.
    from_cache: bool = False
    pages_fetched: list[str] = Field(default_factory=list)


class BookingLinkOut(BaseModel):
    url: str
    platform: BookingChannel
    # False while this platform's URL template is still untested against a
    # live venue. Say so when handing the link over.
    verified: bool
    # The date and time the link asks for, in the restaurant's timezone.
    local_start: datetime


class BookingWindowOut(BaseModel):
    state: WindowState
    # Aware, in the restaurant's timezone. None when the state is unknown.
    opens_at: datetime | None
    # True when nobody recorded the hour the window opens and 09:00 local
    # stood in for it — worth saying out loud before setting a reminder.
    assumed_open_time: bool = False
    window_amount: int | None = None
    window_unit: WindowUnit | None = None


# --- dates -----------------------------------------------------------------


class DateOut(Out):
    id: uuid.UUID
    couple_id: uuid.UUID
    restaurant_id: uuid.UUID | None
    title: str
    start_at: datetime
    end_at: datetime
    party_size: int
    status: DateStatus
    calendar_event_id: str | None
    reminder_event_id: str | None
    booking_url: str | None
    confirmation_number: str | None
    rating: int | None
    notes: str | None


class DatePatch(BaseModel):
    """The fields of a date the skill may change as a booking progresses.

    `status` moves are checked against the state machine in
    `db.models.DATE_TRANSITIONS`; the rest are plain writes. Fields left out
    are left alone.
    """

    model_config = ConfigDict(extra="forbid")

    status: DateStatus | None = None
    title: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    party_size: PartySize | None = None
    restaurant_id: uuid.UUID | None = None
    calendar_event_id: str | None = None
    reminder_event_id: str | None = None
    booking_url: str | None = None
    confirmation_number: str | None = None
    rating: Rating | None = None
    notes: str | None = None
