"""The booking tools the bot's model may call, and which of them pay.

The tools are the MCP server's (`tee_time_booker.mcp`), called in-process
through a FastMCP client, so they are the API's routes and stay in step with
it. The bot narrows them twice:

- `ALLOWED_TOOLS`: what the model is offered at all. Everything a chat needs
  to look and book; not `create_plan`, which edits standing plans.
- `PAYING_TOOLS`: the calls that can spend money. One of these asking for
  `submit_payment` is never run on the model's say-so; the bot holds it until
  the person taps Confirm (see `agent.py`).
"""

import json
from dataclasses import dataclass
from typing import Any

from anthropic.types import ToolParam
from fastmcp import Client

ALLOWED_TOOLS = frozenset(
    {
        "list_courses",
        "get_course",
        "list_plans",
        "get_plan",
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

PAYING_TOOLS = frozenset({"book_plan", "run_request"})


def asks_to_pay(name: str, arguments: dict[str, Any]) -> bool:
    # Anything but an absent or literal False is held: the API coerces "true"
    # or 1 to True, so an `is True` check would let those pay unconfirmed.
    return name in PAYING_TOOLS and arguments.get("submit_payment", False) is not False


@dataclass(frozen=True)
class ToolResult:
    text: str
    is_error: bool = False

    def json(self) -> Any:
        """The route's response body. FastMCP wraps a list as {"result": [...]}."""
        data = json.loads(self.text)
        if isinstance(data, dict) and data.keys() == {"result"}:
            return data["result"]
        return data


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
