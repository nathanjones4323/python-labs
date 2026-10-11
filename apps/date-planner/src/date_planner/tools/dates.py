"""Tools about planned dates and their stops.

A date is a day with an ordered list of stops; every tool here hands back
the whole date, stops in order, so the agent always sees the status those
stops add up to. The calendar events themselves belong to the Google
Calendar connector; what lives here are their ids, one per stop.
"""

import uuid
from datetime import date, time

from fastmcp import FastMCP

from date_planner.db import open_session, repo
from date_planner.db.models import DateStatus
from date_planner.schemas import DateOut, DatePatch, StopIn, StopPatch
from date_planner.tools import as_tool_error, localize


def register(mcp: FastMCP) -> None:
    @mcp.tool
    @as_tool_error
    async def create_date(
        couple_id: uuid.UUID,
        title: str,
        day: date,
        max_leg_minutes: int = 25,
        start_after: time | None = None,
        end_before: time | None = None,
        notes: str | None = None,
    ) -> DateOut:
        """Start a date: a day, with no stops yet. It starts `planning`.

        `max_leg_minutes` is "nearby" for this date — the longest drive or
        walk between consecutive stops before `plan_timeline` flags it. Pick
        it for the area (25 in town; more along the coast) and tell the user
        what you chose. `start_after` / `end_before` bound the whole day, in
        the couple's local time. Add stops with `add_stop`.
        """
        async with open_session() as session:
            row = await repo.create_date(
                session,
                couple_id=couple_id,
                title=title,
                day=day,
                max_leg_minutes=max_leg_minutes,
                start_after=start_after,
                end_before=end_before,
                notes=notes,
            )
            return DateOut.model_validate(row)

    @mcp.tool
    @as_tool_error
    async def update_date(date_id: uuid.UUID, patch: DatePatch) -> DateOut:
        """Change the date's own fields: title, day, window, nearby limit,
        rating, notes.

        Stops change through `add_stop` / `update_stop` / `reorder_stops` /
        `remove_stop`. `status` is derived from the stops, so here it only
        takes `completed` (with a `rating`, afterwards) or `cancelled` — and
        those two stick.
        """
        async with open_session() as session:
            return DateOut.model_validate(
                await repo.update_date(session, date_id, patch)
            )

    @mcp.tool
    @as_tool_error
    async def get_date(date_id: uuid.UUID) -> DateOut:
        """One date with its stops in order, their times, bookings and
        calendar ids."""
        async with open_session() as session:
            return DateOut.model_validate(await repo.get_date(session, date_id))

    @mcp.tool
    @as_tool_error
    async def list_dates(
        couple_id: uuid.UUID,
        status: DateStatus | None = None,
        day_from: date | None = None,
        day_to: date | None = None,
        limit: int = 50,
    ) -> list[DateOut]:
        """The couple's dates with their stops, latest day first.

        Filter by `status` (e.g. `booking_pending` for "what still needs
        booking?") and by a `day_from` / `day_to` range.
        """
        async with open_session() as session:
            rows = await repo.list_dates(
                session,
                couple_id,
                status=status,
                day_from=day_from,
                day_to=day_to,
                limit=limit,
            )
            return [DateOut.model_validate(row) for row in rows]

    @mcp.tool
    @as_tool_error
    async def add_stop(
        date_id: uuid.UUID, stop: StopIn, position: int | None = None
    ) -> DateOut:
        """Add a stop to the day, at `position` (0-based) or at the end.

        `kind`: activity, meal or ticketed. `anchor`: `none` lets
        `plan_timeline` place it; `fixed` keeps its `start_at` (a booked
        reservation or timed entry); `sunset` starts it at sunset +
        `sunset_offset_min` (default -30). Leave `venue_id` out for a slot
        still to be chosen. An already-booked stop: `anchor="fixed"`,
        `booking_status="confirmed"`, with its time. Datetimes without an
        offset are read as the couple's local time.
        """
        async with open_session() as session:
            planned = await repo.get_date(session, date_id)
            tz = (await repo.get_couple(session, planned.couple_id)).tz
            stop = stop.model_copy(
                update={
                    "start_at": stop.start_at and localize(stop.start_at, tz),
                    "end_at": stop.end_at and localize(stop.end_at, tz),
                }
            )
            return DateOut.model_validate(
                await repo.add_stop(session, date_id, stop, position)
            )

    @mcp.tool
    @as_tool_error
    async def update_stop(stop_id: uuid.UUID, patch: StopPatch) -> DateOut:
        """Change one stop: its venue, time, duration, anchor, booking state,
        calendar ids.

        Fields left out are left alone. The date's status is recomputed from
        all its stops afterwards. Typical calls: `venue_id` once a slot is
        chosen; `calendar_event_id` after creating the stop's event;
        `booking_url` + `booking_status="link_sent"`, or `reminder_event_id`
        + `booking_status="reminder_set"`; `confirmation_number` +
        `booking_status="confirmed"` once it's booked.
        """
        async with open_session() as session:
            values = patch.model_dump(exclude_unset=True)
            if values.get("start_at") or values.get("end_at"):
                stop = await repo.get_stop(session, stop_id)
                planned = await repo.get_date(session, stop.date_id)
                tz = (await repo.get_couple(session, planned.couple_id)).tz
                patch = patch.model_copy(
                    update={
                        key: localize(values[key], tz)
                        for key in ("start_at", "end_at")
                        if values.get(key)
                    }
                )
            return DateOut.model_validate(
                await repo.update_stop(session, stop_id, patch)
            )

    @mcp.tool
    @as_tool_error
    async def reorder_stops(date_id: uuid.UUID, stop_ids: list[uuid.UUID]) -> DateOut:
        """Set the order of the day: every stop id of this date, each once."""
        async with open_session() as session:
            return DateOut.model_validate(
                await repo.reorder_stops(session, date_id, stop_ids)
            )

    @mcp.tool
    @as_tool_error
    async def remove_stop(stop_id: uuid.UUID) -> DateOut:
        """Take a stop out of the day; the rest close up.

        Its calendar event isn't touched — retitle it `[CANCELLED]` with the
        Calendar connector if it has one. To keep a record that it was
        dropped, use `update_stop(booking_status="cancelled")` instead.
        """
        async with open_session() as session:
            return DateOut.model_validate(await repo.remove_stop(session, stop_id))
