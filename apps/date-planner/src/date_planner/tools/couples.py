"""Tools about the couple: who they are, what they like, where they've been."""

import uuid

from fastmcp import FastMCP
from sqlalchemy.ext.asyncio import AsyncSession

from date_planner.db import open_session, repo
from date_planner.db.models import Couple
from date_planner.schemas import (
    CoupleProfile,
    CoupleProfileIn,
    DateOut,
    MemberIn,
    MemberProfile,
    PreferenceEdit,
)
from date_planner.tools import as_tool_error


async def couple_profile(session: AsyncSession, couple: Couple) -> CoupleProfile:
    """The couple with its pooled taste and whose turn is next filled in."""
    return CoupleProfile.model_validate(couple).model_copy(
        update={
            "shared": repo.effective_preferences(couple),
            "next_turn": await repo.next_turn(session, couple),
        }
    )


def register(mcp: FastMCP) -> None:
    @mcp.tool
    @as_tool_error
    async def list_couples() -> list[CoupleProfile]:
        """Every couple profile on file, oldest first.

        There is usually exactly one; start here to get its id.
        """
        async with open_session() as session:
            rows = await repo.list_couples(session)
            return [await couple_profile(session, row) for row in rows]

    @mcp.tool
    @as_tool_error
    async def get_couple_profile(couple_id: uuid.UUID) -> CoupleProfile:
        """The couple: budget and travel limit, each member's likes, dislikes
        and dietary needs, and `shared` — the pooled taste to plan a date for
        both by (every like nobody vetoes, every dislike and dietary need of
        either).

        `next_turn` names the member planned for least recently. It's only a
        suggestion: plan for both unless the user says whose turn it is.

        Read this before proposing anything.
        """
        async with open_session() as session:
            return await couple_profile(
                session, await repo.get_couple(session, couple_id)
            )

    @mcp.tool
    @as_tool_error
    async def list_members(couple_id: uuid.UUID) -> list[MemberProfile]:
        """The couple's members with their own likes, dislikes and dietary
        needs. Use these, not `shared`, when a date is one person's pick."""
        async with open_session() as session:
            couple = await repo.get_couple(session, couple_id)
            return [MemberProfile.model_validate(m) for m in couple.members]

    @mcp.tool
    @as_tool_error
    async def upsert_couple_profile(profile: CoupleProfileIn) -> CoupleProfile:
        """Create the couple profile, or update the fields given in `profile`.

        With an `id`, only the fields present are written, so correcting one
        field leaves the rest alone. Without one, a new couple is created and
        `name` is required. Taste isn't here: add the people with
        `upsert_member` and change their preferences with
        `edit_member_preferences`.
        """
        async with open_session() as session:
            return await couple_profile(
                session, await repo.upsert_couple(session, profile)
            )

    @mcp.tool
    @as_tool_error
    async def upsert_member(couple_id: uuid.UUID, member: MemberIn) -> CoupleProfile:
        """Add a member to the couple, or update one's name or email.

        With an `id`, that member is updated; without, a member of the same
        name is, else a new one is added and `name` is required.
        """
        async with open_session() as session:
            await repo.upsert_member(session, couple_id, member)
            return await couple_profile(
                session, await repo.get_couple(session, couple_id)
            )

    @mcp.tool
    @as_tool_error
    async def edit_member_preferences(
        couple_id: uuid.UUID, member: str, edit: PreferenceEdit
    ) -> CoupleProfile:
        """Add or drop single likes, dislikes or dietary needs for one member.

        `member` is their name or email. Use this when a preference changes
        in conversation; the items not named are kept. Liking something takes
        it off that member's dislikes and vice versa; matching ignores case.
        """
        async with open_session() as session:
            return await couple_profile(
                session,
                await repo.edit_preferences(session, couple_id, member, edit),
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
