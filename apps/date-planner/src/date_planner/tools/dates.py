"""Tools about planned dates: the row that tracks one date's progress.

The calendar events themselves belong to the Google Calendar connector; what
lives here are their ids, so a later turn can find the hold it created.
"""

import uuid
from datetime import datetime

from fastmcp import FastMCP

from date_planner.db import open_session, repo
from date_planner.db.models import DateStatus
from date_planner.schemas import DateOut, DatePatch
from date_planner.tools import as_tool_error, localize


def register(mcp: FastMCP) -> None:
    @mcp.tool
    @as_tool_error
    async def create_date(
        couple_id: uuid.UUID,
        title: str,
        start_at: datetime,
        end_at: datetime,
        restaurant_id: uuid.UUID | None = None,
        party_size: int = 2,
        notes: str | None = None,
    ) -> DateOut:
        """Record a date idea. It starts `proposed`.

        `title` is the idea in the couple's words ("Sunset dinner at Nobu
        Malibu"). Leave `restaurant_id` out for a date that isn't a
        restaurant. Datetimes without an offset are read as the couple's
        local time.
        """
        async with open_session() as session:
            couple = await repo.get_couple(session, couple_id)
            tz = couple.tz
            row = await repo.create_date(
                session,
                couple_id=couple_id,
                title=title,
                start_at=localize(start_at, tz),
                end_at=localize(end_at, tz),
                restaurant_id=restaurant_id,
                party_size=party_size,
                notes=notes,
            )
            return DateOut.model_validate(row)

    @mcp.tool
    @as_tool_error
    async def update_date(date_id: uuid.UUID, patch: DatePatch) -> DateOut:
        """Change a date as its booking progresses.

        Fields left out of `patch` are left alone. A `status` has to be a
        move the state machine allows, or you get `InvalidTransition`:

            proposed  -> tentative | booking_pending | cancelled
            tentative -> booking_pending | confirmed | cancelled
            booking_pending -> tentative | confirmed | cancelled
            confirmed -> completed | cancelled

        Typical calls: set `calendar_event_id` and `booking_url` with
        `status="booking_pending"` after creating the hold; set
        `confirmation_number` with `status="confirmed"` once the
        confirmation turns up; set `rating` with `status="completed"`
        afterwards.
        """
        async with open_session() as session:
            return DateOut.model_validate(
                await repo.update_date(session, date_id, patch)
            )

    @mcp.tool
    @as_tool_error
    async def get_date(date_id: uuid.UUID) -> DateOut:
        """One date, with its status, calendar ids and booking link."""
        async with open_session() as session:
            return DateOut.model_validate(await repo.get_date(session, date_id))

    @mcp.tool
    @as_tool_error
    async def list_dates(
        couple_id: uuid.UUID,
        status: DateStatus | None = None,
        start_from: datetime | None = None,
        start_to: datetime | None = None,
        limit: int = 50,
    ) -> list[DateOut]:
        """The couple's dates, soonest-started last, newest first.

        Filter by `status` (e.g. `booking_pending` for "what still needs
        booking?") and by a `start_from` / `start_to` window. Datetimes
        without an offset are read as the couple's local time.
        """
        async with open_session() as session:
            couple = await repo.get_couple(session, couple_id)
            tz = couple.tz
            rows = await repo.list_dates(
                session,
                couple_id,
                status=status,
                start_from=(
                    localize(start_from, tz) if start_from is not None else None
                ),
                start_to=localize(start_to, tz) if start_to is not None else None,
                limit=limit,
            )
            return [DateOut.model_validate(row) for row in rows]
