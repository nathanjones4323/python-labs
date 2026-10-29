"""The booking tools the bot's model may call, and which of them pay.

The tools are the MCP server's (`tee_time_booker.mcp`), called in-process
through a FastMCP client, so they are the API's routes and stay in step with
it. The bot narrows them twice:

- `ALLOWED_TOOLS`: what the model is offered at all: everything a chat needs
  to look, book and schedule.
- `PAYING_TOOLS`: the calls that can spend money, each with the argument that
  makes it do so. Booking now (`submit_payment`) pays at checkout; scheduling
  (`auto_book`, `enabled`) lets the scheduler pay later without asking. One of
  these asking to pay is never run on the model's say-so; the bot holds it
  until the person taps Confirm (see `agent.py`).
"""

from typing import Any

from anthropic.types import ToolParam
from fastmcp import Client

from tee_time_booker.cards import (  # noqa: F401 - re-exported
    PAYING_TOOLS,
    RUN_TOOLS,
    ToolResult,
    asks_to_pay,
)

ALLOWED_TOOLS = frozenset(
    {
        "list_courses",
        "get_course",
        "search_tee_times",
        "list_plans",
        "get_plan",
        "create_plan",
        "set_plan_auto_book",
        "delete_plan",
        "find_tee_times",
        "book_plan",
        "list_requests",
        "create_request",
        "get_request",
        "list_request_runs",
        "run_request",
        "cancel_request",
        "list_runs",
        "get_run",
    }
)


class BookingTools:
    def __init__(self, client: Client) -> None:
        self._client = client

    async def definitions(self) -> list[ToolParam]:
        """The allowed tools as the Messages API wants them, in a fixed order
        so the request prefix (and its cache) doesn't change between turns."""
        tools = await self._client.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description or t.name,
                "input_schema": t.input_schema,
            }
            for t in sorted(tools, key=lambda t: t.name)
            if t.name in ALLOWED_TOOLS
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Run one tool. Not gated: callers check `asks_to_pay` first."""
        if name not in ALLOWED_TOOLS:
            return ToolResult(f"Unknown tool {name!r}", is_error=True)
        result = await self._client.call_tool(name, arguments, raise_on_error=False)
        text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
        return ToolResult(text or "(no content)", is_error=result.is_error)
