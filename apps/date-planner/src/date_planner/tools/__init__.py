"""The MCP tools, grouped by what they're about.

Each module is a thin wrapper: parse the arguments, open one session, call
`db.repo` or `booking.*`, hand back a Pydantic model. The judgement — which
idea to propose, when to invite the partner — lives in the skill, not here.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import wraps
from typing import ParamSpec, TypeVar
from zoneinfo import ZoneInfo

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from date_planner.errors import DatePlannerError

P = ParamSpec("P")
R = TypeVar("R")


def as_tool_error(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Report our own failures as MCP errors the caller can read and branch on.

    The class name leads the message (`NotFound: No couple ...`), so an agent
    sees a code rather than having to parse prose. Anything else is a bug and
    is left to FastMCP, which hides its details from the client.
    """

    @wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except DatePlannerError as exc:
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    return wrapper


def localize(moment: datetime, tz: ZoneInfo) -> datetime:
    """Read `moment` in `tz`.

    A naive datetime is taken to be local time there — an agent saying
    "7pm on the 24th" means 7pm at the venue, not 7pm UTC.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=tz)
    return moment.astimezone(tz)


def register_all(mcp: FastMCP) -> None:
    from date_planner.tools import couples, dates, planning, venues

    couples.register(mcp)
    venues.register(mcp)
    dates.register(mcp)
    planning.register(mcp)
