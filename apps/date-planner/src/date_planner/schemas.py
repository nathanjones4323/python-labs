"""What the tools take and hand back.

These are the MCP tool signatures, so they double as the schema an agent
reads. Output models are built straight off the ORM rows
(`from_attributes`); input models are permissive about what they leave out,
because the skill fills a profile in over several turns.
"""

import uuid
from datetime import date, datetime, time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from date_planner.booking.channels import (
    BookingChannel,
    WindowState,
    WindowUnit,
)
from date_planner.db.models import (
    Anchor,
    DateStatus,
    ReservationNeed,
    StopBookingStatus,
    StopKind,
    TravelMode,
    VenueType,
)

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
    doesn't wipe the rest. A list given here replaces the stored one whole;
    to add or drop single items use `PreferenceEdit`.
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


class PreferenceEdit(BaseModel):
    """Items to add to or drop from the couple's preference lists.

    Matching ignores case and surrounding spaces. Liking something takes it
    off `dislikes`, and disliking it takes it off `likes`, so "we've gone off
    sushi" is one `add_dislikes`. Removals run before additions.
    """

    model_config = ConfigDict(extra="forbid")

    add_likes: list[str] = Field(default_factory=list)
    remove_likes: list[str] = Field(default_factory=list)
    add_dislikes: list[str] = Field(default_factory=list)
    remove_dislikes: list[str] = Field(default_factory=list)
    add_dietary: list[str] = Field(default_factory=list)
    remove_dietary: list[str] = Field(default_factory=list)


# --- venues ----------------------------------------------------------------


class VenueOut(Out):
    id: uuid.UUID
    name: str
    city: str
    venue_type: VenueType
    address: str | None
    phone: str | None
    website: str | None
    timezone: str
    lat: float | None
    lng: float | None
    external_url: str | None
    default_duration_min: int | None
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


class VenueIn(BaseModel):
    """A venue to insert, or — with `id` — one to update.

    Without an `id`, `name` and `city` identify the row: a second upsert of
    "Nobu Malibu" in "Malibu" updates the first one rather than duplicating
    it. A new venue needs `venue_type`.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    name: str | None = None
    city: str | None = None
    venue_type: VenueType | None = None
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    timezone: str | None = None
    lat: Annotated[float, Field(ge=-90, le=90)] | None = None
    lng: Annotated[float, Field(ge=-180, le=180)] | None = None
    external_url: str | None = None
    default_duration_min: Annotated[int, Field(gt=0)] | None = None
    booking_channel: BookingChannel | None = None
    platform_slug: str | None = None
    platform_city_slug: str | None = None
    window_amount: int | None = None
    window_unit: WindowUnit | None = None
    window_open_time: time | None = None
    notes: str | None = None


class DetectionOut(BaseModel):
    """What reading the venue's website said about how it books."""

    venue_id: uuid.UUID | None = None
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
    # live venue — and always for `ticketed`, whose page doesn't prefill.
    verified: bool
    # The date and time the link asks for, in the venue's timezone.
    local_start: datetime


class BookingWindowOut(BaseModel):
    state: WindowState
    # Aware, in the venue's timezone. None when the state is unknown.
    opens_at: datetime | None
    # True when nobody recorded the hour the window opens and 09:00 local
    # stood in for it — worth saying out loud before setting a reminder.
    assumed_open_time: bool = False
    window_amount: int | None = None
    window_unit: WindowUnit | None = None


class ReservationNeedOut(BaseModel):
    """The default need for a stop, from how its venue books.

    Raise it if you know better; don't lower it without a reason.
    """

    stop_id: uuid.UUID
    channel: BookingChannel
    suggested: ReservationNeed
    reason: str


# --- dates and stops -------------------------------------------------------


class StopOut(Out):
    id: uuid.UUID
    position: int
    title: str
    kind: StopKind
    venue_id: uuid.UUID | None
    venue_name: str | None
    party_size: int
    start_at: datetime | None
    end_at: datetime | None
    est_duration_min: int | None
    anchor: Anchor
    sunset_offset_min: int
    reservation_need: ReservationNeed
    booking_status: StopBookingStatus
    booking_url: str | None
    confirmation_number: str | None
    calendar_event_id: str | None
    reminder_event_id: str | None
    travel_min_from_prev: int | None
    travel_mode: TravelMode
    notes: str | None


class DateOut(Out):
    id: uuid.UUID
    couple_id: uuid.UUID
    title: str
    day: date
    max_leg_minutes: int
    start_after: time | None
    end_before: time | None
    # Derived from the stops, except `completed` / `cancelled`.
    status: DateStatus
    rating: int | None
    notes: str | None
    # The published preview page; republish to it after a change.
    preview_url: str | None
    stops: list[StopOut]


class StopIn(BaseModel):
    """A stop to add to a date.

    `booking_status` defaults from `reservation_need`: `not_needed` when the
    need is `none`, otherwise `needed` — an unknown need holds the date back
    from `confirmed` until someone looks. Datetimes without an offset are read
    as the couple's local time.
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    kind: StopKind
    venue_id: uuid.UUID | None = None
    party_size: PartySize = 2
    start_at: datetime | None = None
    end_at: datetime | None = None
    est_duration_min: Annotated[int, Field(gt=0)] | None = None
    anchor: Anchor = Anchor.NONE
    sunset_offset_min: int = -30
    reservation_need: ReservationNeed = ReservationNeed.UNKNOWN
    booking_status: StopBookingStatus | None = None
    booking_url: str | None = None
    confirmation_number: str | None = None
    calendar_event_id: str | None = None
    reminder_event_id: str | None = None
    travel_mode: TravelMode = TravelMode.DRIVING
    notes: str | None = None


class StopPatch(BaseModel):
    """The fields of a stop to change; those left out are left alone.

    The date's status is worked out again after every change.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    kind: StopKind | None = None
    venue_id: uuid.UUID | None = None
    party_size: PartySize | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    est_duration_min: Annotated[int, Field(gt=0)] | None = None
    anchor: Anchor | None = None
    sunset_offset_min: int | None = None
    reservation_need: ReservationNeed | None = None
    booking_status: StopBookingStatus | None = None
    booking_url: str | None = None
    confirmation_number: str | None = None
    calendar_event_id: str | None = None
    reminder_event_id: str | None = None
    travel_mode: TravelMode | None = None
    notes: str | None = None


class DatePatch(BaseModel):
    """The date's own fields; stops change through the stop tools.

    `status` is derived from the stops, so the only values it takes here are
    the two set by hand: `completed` and `cancelled`.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "cancelled"] | None = None
    title: str | None = None
    day: date | None = None
    max_leg_minutes: Annotated[int, Field(gt=0)] | None = None
    start_after: time | None = None
    end_before: time | None = None
    rating: Rating | None = None
    notes: str | None = None
    # The Artifact link the preview page was published to.
    preview_url: str | None = None


# --- planning --------------------------------------------------------------


class SunsetOut(BaseModel):
    """All in the venue's (or the given) timezone."""

    sunset: datetime
    civil_dusk: datetime
    golden_hour_start: datetime


class TravelOut(BaseModel):
    """One leg. `minutes` includes the driving padding and is rounded up."""

    minutes: int
    meters: int
    provider: str
    # True for the haversine estimate; false for a routed answer.
    is_estimate: bool
    from_cache: bool = False


class PlacedStopOut(BaseModel):
    id: uuid.UUID
    title: str
    start_at: datetime | None
    end_at: datetime | None
    travel_min_from_prev: int | None


class LegOut(BaseModel):
    from_stop_id: uuid.UUID
    to_stop_id: uuid.UUID
    # Null when either end has no venue or no coordinates.
    minutes: int | None
    meters: int | None
    provider: str | None
    is_estimate: bool | None
    mode: TravelMode


class ViolationOut(BaseModel):
    type: str
    message: str
    stop_id: uuid.UUID | None = None


class PreviewOut(BaseModel):
    """A date's preview page, written to disk for the session to publish."""

    # Publish this with the Artifact tool as `file_path`...
    page_path: str
    # ...and these as its `files` ({published path: local path}): the
    # basemap, one image per theme. Empty when the tiles couldn't be had.
    files: dict[str, str]
    # Set once the page has been published: pass it as the Artifact `url`.
    preview_url: str | None
    stops_on_map: int
    # Stops left off the map: no venue yet, or a venue with no lat/lng.
    missing_coordinates: list[str]
    # False when the tiles couldn't be fetched; the pins sit on a plain grid.
    basemap: bool


class TimelineOut(BaseModel):
    stops: list[PlacedStopOut]
    legs: list[LegOut]
    # At the first stop with coordinates; null if none has any.
    sunset: datetime | None
    max_leg_minutes: int
    violations: list[ViolationOut]
    # True when the times above were written to the stops.
    applied: bool
