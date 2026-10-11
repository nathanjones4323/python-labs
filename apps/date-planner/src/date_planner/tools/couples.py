"""Tools about the couple: who they are, what they like, where they've been."""

import uuid

from fastmcp import FastMCP

from date_planner.db import open_session, repo
from date_planner.schemas import (
    CoupleProfile,
    CoupleProfileIn,
    DateOut,
    PreferenceEdit,
)
from date_planner.tools import as_tool_error


def register(mcp: FastMCP) -> None:
    @mcp.tool
    @as_tool_error
    async def list_couples() -> list[CoupleProfile]:
        """Every couple profile on file, oldest first.

        There is usually exactly one; start here to get its id.
        """
        async with open_session() as session:
            rows = await repo.list_couples(session)
            return [CoupleProfile.model_validate(row) for row in rows]

    @mcp.tool
    @as_tool_error
    async def get_couple_profile(couple_id: uuid.UUID) -> CoupleProfile:
        """The couple's preferences: budget, dietary needs, likes, travel limit.

        Read this before proposing anything.
        """
        async with open_session() as session:
            return CoupleProfile.model_validate(
                await repo.get_couple(session, couple_id)
            )

    @mcp.tool
    @as_tool_error
    async def upsert_couple_profile(profile: CoupleProfileIn) -> CoupleProfile:
        """Create the couple profile, or update the fields given in `profile`.

        With an `id`, only the fields present are written, so correcting one
        preference leaves the rest alone. Without one, a new couple is
        created and `name` is required.

        A list field (`likes`, `dislikes`, `dietary`) replaces the stored
        list whole. To add or drop one item, use `edit_couple_preferences`.
        """
        async with open_session() as session:
            return CoupleProfile.model_validate(
                await repo.upsert_couple(session, profile)
            )

    @mcp.tool
    @as_tool_error
    async def edit_couple_preferences(
        couple_id: uuid.UUID, edit: PreferenceEdit
    ) -> CoupleProfile:
        """Add or drop single likes, dislikes or dietary needs.

        Use this, not `upsert_couple_profile`, when a preference changes in
        conversation: the items not named are kept. Liking something takes
        it off the dislikes and vice versa; matching ignores case.
        """
        async with open_session() as session:
            return CoupleProfile.model_validate(
                await repo.edit_preferences(session, couple_id, edit)
            )

    @mcp.tool
    @as_tool_error
    async def list_past_dates(couple_id: uuid.UUID, limit: int = 20) -> list[DateOut]:
        """Dates up to today, most recent first, with ratings and their stops.

        Use it to avoid proposing a repeat — a trail or museum as much as a
        restaurant — and to lean towards what they rated highly. Cancelled
        dates aren't included: they never happened.
        """
        async with open_session() as session:
            rows = await repo.list_past_dates(session, couple_id, limit=limit)
            return [DateOut.model_validate(row) for row in rows]
