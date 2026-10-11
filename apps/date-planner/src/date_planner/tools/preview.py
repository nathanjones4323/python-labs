"""The date preview: a page to look at, not a record.

The server writes the page; the session publishes it with its Artifact tool
and saves the link back with `update_date(preview_url=...)`.
"""

import uuid

import httpx
from fastmcp import FastMCP

from date_planner import preview
from date_planner.config import get_settings
from date_planner.db import open_session, repo
from date_planner.schemas import PreviewOut
from date_planner.tools import as_tool_error


def register(mcp: FastMCP) -> None:
    @mcp.tool
    @as_tool_error
    async def render_date_preview(date_id: uuid.UUID) -> PreviewOut:
        """Write the date's preview page: a map of the stops with the route
        between them, then each stop's time, drive or walk, booking state, and
        where sunset falls. Made for a phone.

        Nothing is published here. Publish `page_path` with the Artifact
        tool, passing `files` as its supporting files, and `preview_url` as
        its `url` when it's set (read that artifact first). After a first
        publish, save the link with `update_date(preview_url=...)`. Run it
        again after any change to the stops; it overwrites the same files.
        """
        settings = get_settings()
        async with open_session() as session:
            planned = await repo.get_date(session, date_id)
            tz = (await repo.get_couple(session, planned.couple_id)).tz
            async with httpx.AsyncClient(
                timeout=settings.fetch_timeout,
                headers={"User-Agent": settings.map_user_agent},
            ) as client:
                written = await preview.write(planned, tz, settings, client)
            return PreviewOut(
                page_path=str(written.page),
                files={name: str(path) for name, path in written.files.items()},
                preview_url=planned.preview_url,
                stops_on_map=written.stops_on_map,
                missing_coordinates=written.missing_coordinates,
                basemap=written.basemap,
            )
