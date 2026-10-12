"""The queries the tools run.

Every function takes an open `AsyncSession` and leaves committing to the
caller, so one tool call is one transaction. Lookups that an agent can
recover from raise `errors.NotFound` rather than returning None; the one
exception is `find_venue`, whose whole job is to answer "do we know this
place?".

Every stop change ends in `_settle`, which reloads the date's stops in order
and stores the status they add up to (`models.derive_status`).
"""

import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from date_planner.booking.detect import Detection
from date_planner.db.models import (
    PENDING_BOOKING,
    STICKY_STATUSES,
    Couple,
    CoupleMember,
    DateStatus,
    DateStop,
    PlannedDate,
    ReservationNeed,
    StopBookingStatus,
    TravelCache,
    TravelMode,
    Venue,
    derive_status,
)
from date_planner.errors import InvalidTransition, NotFound
from date_planner.schemas import (
    CoupleProfileIn,
    DatePatch,
    MemberIn,
    NextTurn,
    PreferenceEdit,
    Preferences,
    StopIn,
    StopPatch,
    VenueIn,
)


def _assign(row: object, values: dict[str, object], required: frozenset[str]) -> None:
    """Write `values` onto `row`.

    An explicit null clears a field — that's how a preference gets removed —
    except for the columns in `required`, which have no sensible empty value
    and are left as they are.
    """
    for field, value in values.items():
        if value is None and field in required:
            continue
        setattr(row, field, value)


# --- couples ---------------------------------------------------------------


# Columns with no sensible empty value; see `_assign`.
COUPLE_REQUIRED = frozenset({"name", "timezone"})


async def get_couple(session: AsyncSession, couple_id: uuid.UUID) -> Couple:
    couple = await session.get(Couple, couple_id)
    if couple is None:
        raise NotFound(f"No couple {couple_id}")
    return couple


async def date_tz(session: AsyncSession, planned: PlannedDate) -> ZoneInfo:
    """The timezone a date is planned in: its couple's."""
    return (await get_couple(session, planned.couple_id)).tz


async def list_couples(session: AsyncSession) -> list[Couple]:
    result = await session.scalars(select(Couple).order_by(Couple.created_at))
    return list(result)


async def upsert_couple(session: AsyncSession, profile: CoupleProfileIn) -> Couple:
    """Update the couple `profile.id` names, or insert a new one."""
    values = profile.model_dump(exclude_unset=True, exclude={"id"})
    if profile.id is not None:
        couple = await get_couple(session, profile.id)
    else:
        if not values.get("name"):
            raise ValueError("A new couple needs a name")
        # `members` set so it reads as loaded: an async session can't lazy-load it.
        couple = Couple(name=values.pop("name"), members=[])
        session.add(couple)
    _assign(couple, values, COUPLE_REQUIRED)
    await session.flush()
    return couple


def _key(item: str) -> str:
    return item.strip().casefold()


def _without(items: list[str], drop: list[str]) -> list[str]:
    dropped = {_key(d) for d in drop}
    return [item for item in items if _key(item) not in dropped]


def _with(items: list[str], add: list[str]) -> list[str]:
    seen = {_key(item) for item in items}
    out = list(items)
    for item in add:
        if item.strip() and _key(item) not in seen:
            seen.add(_key(item))
            out.append(item.strip())
    return out


async def resolve_member(
    session: AsyncSession, couple_id: uuid.UUID, ref: str
) -> CoupleMember:
    """The couple's member `ref` names: by name, email or id, ignoring case."""
    couple = await get_couple(session, couple_id)
    wanted = _key(ref)
    for member in couple.members:
        if wanted in {
            _key(member.name),
            _key(member.email or ""),
            str(member.id),
        }:
            return member
    known = ", ".join(m.name for m in couple.members) or "none yet"
    raise NotFound(f"No member {ref!r} in couple {couple_id} (members: {known})")


async def upsert_member(
    session: AsyncSession, couple_id: uuid.UUID, member: MemberIn
) -> CoupleMember:
    """Update the member `member.id` (or the same name) names, or add one."""
    couple = await get_couple(session, couple_id)
    values = member.model_dump(exclude_unset=True, exclude={"id"})
    row: CoupleMember | None = None
    if member.id is not None:
        row = next((m for m in couple.members if m.id == member.id), None)
        if row is None:
            raise NotFound(f"No member {member.id} in couple {couple_id}")
    elif values.get("name"):
        row = next(
            (m for m in couple.members if _key(m.name) == _key(values["name"])), None
        )
        if row is not None:
            values.pop("name")  # matched by it; don't re-case the stored one
    if row is None:
        if not values.get("name"):
            raise ValueError("A new member needs a name")
        row = CoupleMember(
            couple_id=couple_id,
            name=values.pop("name"),
            likes=[],
            dislikes=[],
            dietary=[],
        )
        couple.members.append(row)
    _assign(row, values, frozenset({"name"}))
    await session.flush()
    return row


async def edit_preferences(
    session: AsyncSession, couple_id: uuid.UUID, member_ref: str, edit: PreferenceEdit
) -> Couple:
    """Add and drop single preference items on one member; see `PreferenceEdit`.

    Each list is assigned anew rather than mutated, so the ORM sees the
    change to the ARRAY column. Returns the couple, so both members show.
    """
    member = await resolve_member(session, couple_id, member_ref)
    likes = _without(member.likes, edit.remove_likes + edit.add_dislikes)
    dislikes = _without(member.dislikes, edit.remove_dislikes + edit.add_likes)
    member.likes = _with(likes, edit.add_likes)
    member.dislikes = _with(dislikes, edit.add_dislikes)
    member.dietary = _with(
        _without(member.dietary, edit.remove_dietary), edit.add_dietary
    )
    await session.flush()
    return await get_couple(session, couple_id)


def _pool(lists: list[list[str]]) -> list[str]:
    out: list[str] = []
    for items in lists:
        out = _with(out, items)
    return out


def effective_preferences(
    couple: Couple, member: CoupleMember | None = None
) -> Preferences:
    """The taste to plan a date by.

    Dislikes and dietary needs are always pooled across the members: a date
    for one person still has to work for the other. Likes are the member's
    own when a date is theirs, else everyone's; either way a like that
    anybody dislikes is left out.
    """
    dislikes = _pool([m.dislikes for m in couple.members])
    dietary = _pool([m.dietary for m in couple.members])
    liked = (
        member.likes if member is not None else _pool([m.likes for m in couple.members])
    )
    return Preferences(
        likes=_without(liked, dislikes), dislikes=dislikes, dietary=dietary
    )


async def next_turn(session: AsyncSession, couple: Couple) -> NextTurn | None:
    """The member planned for least recently, never-planned first.

    Dates planned for both don't count, and neither do cancelled ones — they
    never happened. Ties go to the member who was added first.
    """
    if not couple.members:
        return None
    result = await session.execute(
        select(PlannedDate.planned_for_member_id, func.max(PlannedDate.day))
        .where(
            PlannedDate.couple_id == couple.id,
            PlannedDate.planned_for_member_id.is_not(None),
            PlannedDate.status != DateStatus.CANCELLED,
        )
        .group_by(PlannedDate.planned_for_member_id)
    )
    last = {member_id: day for member_id, day in result}
    # min() keeps the first of equals, and `members` is in the order added.
    chosen = min(couple.members, key=lambda m: last.get(m.id, date.min))
    return NextTurn(
        member_id=chosen.id, name=chosen.name, last_planned_for=last.get(chosen.id)
    )


# --- venues ----------------------------------------------------------------


VENUE_REQUIRED = frozenset(
    {"name", "city", "venue_type", "timezone", "booking_channel"}
)


async def get_venue(session: AsyncSession, venue_id: uuid.UUID) -> Venue:
    venue = await session.get(Venue, venue_id)
    if venue is None:
        raise NotFound(f"No venue {venue_id}")
    return venue


async def list_venues(session: AsyncSession) -> list[Venue]:
    result = await session.scalars(select(Venue).order_by(Venue.name))
    return list(result)


async def find_venue(session: AsyncSession, name: str, city: str) -> Venue | None:
    """The venue with this name in this city, whatever the capitalisation."""
    found: Venue | None = await session.scalar(
        select(Venue).where(
            func.lower(Venue.name) == name.strip().lower(),
            func.lower(Venue.city) == city.strip().lower(),
        )
    )
    return found


async def upsert_venue(session: AsyncSession, data: VenueIn) -> Venue:
    """Update the venue `data.id` — or `(name, city)` — names, else insert it."""
    values = data.model_dump(exclude_unset=True, exclude={"id"})
    venue: Venue | None = None
    if data.id is not None:
        venue = await get_venue(session, data.id)
    elif data.name and data.city:
        venue = await find_venue(session, data.name, data.city)
    if venue is None:
        if not data.name or not data.city:
            raise ValueError("A new venue needs a name and a city")
        if data.venue_type is None:
            raise ValueError("A new venue needs a venue_type")
        venue = Venue(
            name=data.name.strip(), city=data.city.strip(), venue_type=data.venue_type
        )
        session.add(venue)
        for key in ("name", "city", "venue_type"):
            values.pop(key, None)
    if values.get("google_place_id", venue.google_place_id) != venue.google_place_id:
        # The saved Places response was for the old id.
        venue.google_place_data = None
    _assign(venue, values, VENUE_REQUIRED)
    await session.flush()
    return venue


async def record_detection(
    session: AsyncSession, venue: Venue, detection: Detection
) -> Venue:
    """Cache what detection found on the venue row.

    Only fills in a phone number or a booking window that wasn't there: a
    value a person typed in beats one a regex read off a web page.
    """
    venue.booking_channel = detection.channel
    venue.channel_confidence = detection.confidence
    venue.channel_evidence = detection.evidence
    venue.channel_checked_at = datetime.now(timezone.utc)
    if detection.platform_slug:
        venue.platform_slug = detection.platform_slug
    if detection.platform_city_slug:
        venue.platform_city_slug = detection.platform_city_slug
    if detection.phone and not venue.phone:
        venue.phone = detection.phone
    window = detection.window
    if window is not None and venue.window_amount is None:
        venue.window_amount = window.amount
        venue.window_unit = window.unit
        if window.open_time is not None and venue.window_open_time is None:
            venue.window_open_time = window.open_time
    await session.flush()
    return venue


# --- dates -----------------------------------------------------------------


DATE_REQUIRED = frozenset({"title", "day", "max_leg_minutes"})


async def get_date(session: AsyncSession, date_id: uuid.UUID) -> PlannedDate:
    planned = await session.get(PlannedDate, date_id)
    if planned is None:
        raise NotFound(f"No date {date_id}")
    return planned


async def _settle(session: AsyncSession, planned: PlannedDate) -> PlannedDate:
    """Reload the stops in order and store the status they add up to.

    The stops' venues are reloaded too, in one query: a new stop, or one
    whose `venue_id` just changed, would otherwise leave `venue` unloaded or
    stale — and an async session can't lazy-load it when the output reads
    `venue_name`.
    """
    await session.flush()
    stops = await session.scalars(
        select(DateStop)
        .where(DateStop.date_id == planned.id)
        .order_by(DateStop.position)
        .options(selectinload(DateStop.venue))
        .execution_options(populate_existing=True)
    )
    set_committed_value(planned, "stops", list(stops))
    planned.status = derive_status(planned.status, planned.stops)
    await session.flush()
    return planned


def _check_window(start_after: time | None, end_before: time | None) -> None:
    if start_after is not None and end_before is not None and end_before <= start_after:
        raise ValueError("end_before must be after start_after")


async def create_date(
    session: AsyncSession,
    *,
    couple_id: uuid.UUID,
    title: str,
    day: date,
    max_leg_minutes: int = 25,
    start_after: time | None = None,
    end_before: time | None = None,
    notes: str | None = None,
    planned_for: str | None = None,
) -> PlannedDate:
    await get_couple(session, couple_id)  # 404 now, not on a later update
    _check_window(start_after, end_before)
    member = (
        await resolve_member(session, couple_id, planned_for)
        if planned_for is not None
        else None
    )
    planned = PlannedDate(
        couple_id=couple_id,
        planned_for_member=member,
        title=title,
        day=day,
        max_leg_minutes=max_leg_minutes,
        start_after=start_after,
        end_before=end_before,
        notes=notes,
        status=DateStatus.PLANNING,
    )
    session.add(planned)
    return await _settle(session, planned)


# Booking progress made for a particular day; it doesn't carry to another.
DAY_BOUND_BOOKING = PENDING_BOOKING | {StopBookingStatus.CONFIRMED}


def _shift(at: datetime | None, by: timedelta, tz: ZoneInfo) -> datetime | None:
    """`at` moved `by` whole days on the wall clock, so 7pm stays 7pm
    across a DST change."""
    if at is None:
        return None
    local = at.astimezone(tz).replace(tzinfo=None)
    return (local + by).replace(tzinfo=tz)


def _move_stops(stops: list[DateStop], by: timedelta, tz: ZoneInfo) -> None:
    """Move the stops' times `by` days, in the couple's timezone.

    A booking was made for the old day, so a stop that had one in hand or
    under way goes back to `needed` and loses its prefilled link. The
    confirmation number stays: it's what cancels the old booking.
    """
    for stop in stops:
        stop.start_at = _shift(stop.start_at, by, tz)
        stop.end_at = _shift(stop.end_at, by, tz)
        if stop.booking_status in DAY_BOUND_BOOKING:
            stop.booking_status = StopBookingStatus.NEEDED
            stop.booking_url = None


async def update_date(
    session: AsyncSession, date_id: uuid.UUID, patch: DatePatch
) -> PlannedDate:
    """Apply `patch` to the date's own fields.

    A new `day` takes the stops with it; see `_move_stops`.

    `status` only takes `completed` or `cancelled`, and once a date is in
    either it stays there.
    """
    planned = await get_date(session, date_id)
    values = patch.model_dump(exclude_unset=True)
    status = values.pop("status", None)
    if "planned_for" in values:
        ref = values.pop("planned_for")
        planned.planned_for_member = (
            await resolve_member(session, planned.couple_id, ref)
            if ref is not None
            else None
        )
    new_day = values.get("day")
    if new_day is not None and new_day != planned.day:
        _move_stops(
            planned.stops, new_day - planned.day, await date_tz(session, planned)
        )
    _assign(planned, values, DATE_REQUIRED)
    _check_window(planned.start_after, planned.end_before)
    if status is not None:
        target = DateStatus(status)
        if planned.status in STICKY_STATUSES and target != planned.status:
            raise InvalidTransition(
                f"Date {planned.id} is {planned.status}; it can't become {target}"
            )
        planned.status = target
    return await _settle(session, planned)


async def list_dates(
    session: AsyncSession,
    couple_id: uuid.UUID,
    *,
    status: DateStatus | None = None,
    day_from: date | None = None,
    day_to: date | None = None,
    limit: int = 50,
) -> list[PlannedDate]:
    query = (
        select(PlannedDate)
        .where(PlannedDate.couple_id == couple_id)
        .order_by(PlannedDate.day.desc(), PlannedDate.created_at.desc())
        .limit(limit)
    )
    if status is not None:
        query = query.where(PlannedDate.status == status)
    if day_from is not None:
        query = query.where(PlannedDate.day >= day_from)
    if day_to is not None:
        query = query.where(PlannedDate.day <= day_to)
    result = await session.scalars(query)
    return list(result)


async def list_past_dates(
    session: AsyncSession,
    couple_id: uuid.UUID,
    *,
    limit: int = 20,
    today: date | None = None,
) -> list[PlannedDate]:
    """Dates on or before today in the couple's timezone, most recent first.

    This is what keeps the skill from proposing last Friday's venues again —
    the stops come along, so a repeat trail or museum shows up as well as a
    repeat restaurant. Cancelled dates are left out: they never happened.
    """
    if today is None:
        couple = await get_couple(session, couple_id)
        today = datetime.now(couple.tz).date()
    result = await session.scalars(
        select(PlannedDate)
        .where(
            PlannedDate.couple_id == couple_id,
            PlannedDate.day <= today,
            PlannedDate.status != DateStatus.CANCELLED,
        )
        .order_by(PlannedDate.day.desc(), PlannedDate.created_at.desc())
        .limit(limit)
    )
    return list(result)


# --- stops -----------------------------------------------------------------


STOP_REQUIRED = frozenset(
    {
        "title",
        "kind",
        "party_size",
        "anchor",
        "sunset_offset_min",
        "reservation_need",
        "booking_status",
        "travel_mode",
    }
)


async def get_stop(session: AsyncSession, stop_id: uuid.UUID) -> DateStop:
    stop = await session.get(DateStop, stop_id)
    if stop is None:
        raise NotFound(f"No stop {stop_id}")
    return stop


def _check_times(stop: DateStop) -> None:
    if stop.start_at is not None and stop.end_at is not None:
        if stop.end_at <= stop.start_at:
            raise ValueError("end_at must be after start_at")


def default_booking_status(need: ReservationNeed) -> StopBookingStatus:
    """`not_needed` only when nothing needs booking; anything else waits."""
    if need is ReservationNeed.NONE:
        return StopBookingStatus.NOT_NEEDED
    return StopBookingStatus.NEEDED


async def add_stop(
    session: AsyncSession,
    date_id: uuid.UUID,
    data: StopIn,
    position: int | None = None,
) -> PlannedDate:
    """Insert a stop at `position` (0-based), or at the end of the day.

    The stops already at or after `position` move down one.
    """
    planned = await get_date(session, date_id)
    if data.venue_id is not None:
        await get_venue(session, data.venue_id)
    count = len(planned.stops)
    at = count if position is None else max(0, min(position, count))
    for existing in planned.stops:
        if existing.position >= at:
            existing.position += 1
    stop = DateStop(
        **data.model_dump(exclude={"booking_status"}),
        position=at,
        booking_status=data.booking_status
        or default_booking_status(data.reservation_need),
    )
    _check_times(stop)
    planned.stops.append(stop)
    return await _settle(session, planned)


async def update_stop(
    session: AsyncSession, stop_id: uuid.UUID, patch: StopPatch
) -> PlannedDate:
    stop = await get_stop(session, stop_id)
    values = patch.model_dump(exclude_unset=True)
    if values.get("venue_id") is not None:
        await get_venue(session, values["venue_id"])
    # A new need moves a booking_status that was only ever the old need's
    # default; otherwise a stop added as `unknown` and later found to need
    # nothing would hold the date at `booking_pending` for good.
    new_need = values.get("reservation_need")
    if (
        new_need is not None
        and "booking_status" not in values
        and stop.booking_status is default_booking_status(stop.reservation_need)
    ):
        values["booking_status"] = default_booking_status(new_need)
    _assign(stop, values, STOP_REQUIRED)
    _check_times(stop)
    return await _settle(session, await get_date(session, stop.date_id))


async def reorder_stops(
    session: AsyncSession, date_id: uuid.UUID, stop_ids: list[uuid.UUID]
) -> PlannedDate:
    """Put the date's stops in the order given: every stop, each once."""
    planned = await get_date(session, date_id)
    by_id = {stop.id: stop for stop in planned.stops}
    if len(stop_ids) != len(set(stop_ids)) or set(stop_ids) != set(by_id):
        raise ValueError(
            "stop_ids must list every stop of this date exactly once: "
            + ", ".join(str(i) for i in by_id)
        )
    for position, stop_id in enumerate(stop_ids):
        by_id[stop_id].position = position
    return await _settle(session, planned)


async def remove_stop(session: AsyncSession, stop_id: uuid.UUID) -> PlannedDate:
    """Delete a stop and close the gap it leaves in the order."""
    stop = await get_stop(session, stop_id)
    planned = await get_date(session, stop.date_id)
    planned.stops.remove(stop)
    remaining = sorted(planned.stops, key=lambda s: s.position)
    for position, kept in enumerate(remaining):
        kept.position = position
    return await _settle(session, planned)


# Per stop: (start_at, end_at, travel_min_from_prev).
StopTimes = tuple[datetime | None, datetime | None, int | None]


async def write_timeline(
    session: AsyncSession, planned: PlannedDate, times: dict[uuid.UUID, StopTimes]
) -> PlannedDate:
    """Store what `plan_timeline` worked out."""
    for stop in planned.stops:
        if stop.id in times:
            stop.start_at, stop.end_at, stop.travel_min_from_prev = times[stop.id]
    return await _settle(session, planned)


# --- travel cache ----------------------------------------------------------


async def cached_travel(
    session: AsyncSession,
    from_venue_id: uuid.UUID,
    to_venue_id: uuid.UUID,
    mode: TravelMode,
    provider: str,
    *,
    ttl_days: int,
    now: datetime | None = None,
) -> TravelCache | None:
    """The cached leg, unless it's `ttl_days` old or older."""
    # populate_existing: `cache_travel` upserts around the identity map, so a
    # copy already in this session may be stale.
    row = await session.get(
        TravelCache,
        (from_venue_id, to_venue_id, mode, provider),
        populate_existing=True,
    )
    if row is None:
        return None
    now = now or datetime.now(timezone.utc)
    if now - row.computed_at >= timedelta(days=ttl_days):
        return None
    return row


async def cache_travel(
    session: AsyncSession,
    from_venue_id: uuid.UUID,
    to_venue_id: uuid.UUID,
    mode: TravelMode,
    provider: str,
    *,
    minutes: float,
    meters: float,
) -> None:
    """Store a leg, replacing an expired one for the same key."""
    now = datetime.now(timezone.utc)
    statement = insert(TravelCache).values(
        from_venue_id=from_venue_id,
        to_venue_id=to_venue_id,
        mode=mode,
        provider=provider,
        minutes=minutes,
        meters=meters,
        computed_at=now,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["from_venue_id", "to_venue_id", "mode", "provider"],
            set_={"minutes": minutes, "meters": meters, "computed_at": now},
        )
    )
