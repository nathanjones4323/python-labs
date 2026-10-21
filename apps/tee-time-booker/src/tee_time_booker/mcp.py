"""The API's routes as MCP tools, for an agent to book tee times with.

    python -m tee_time_booker.mcp             # stdio, for Claude Desktop / Claude Code
    python -m tee_time_booker.mcp --http      # Streamable HTTP on :8001

FastMCP reads the app's OpenAPI schema and calls the routes in-process, so
the tool names are the routes' `operation_id`s (`list_plans`, `book_plan`,
`get_run`, ...) and their descriptions are the routes' docs. Nothing here
knows what the tools do; change the API and the tools follow.

Needs the `mcp` extra: `uv sync --package tee-time-booker --extra mcp`.
"""

import sys

from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType, RouteMap

from tee_time_booker.api import create_app


def create_server(app: FastAPI | None = None) -> FastMCP:
    """The tools over `app` (default: one built from the environment).

    Pass the app to keep hold of it: its `state.worker` runs the bookings,
    and a long-lived host (the Telegram bot) shuts that down itself, since
    nothing here runs the app's lifespan.
    """
    return FastMCP.from_fastapi(
        app or create_app(),
        name="tee-time-booker",
        route_maps=[
            RouteMap(pattern=r"^/health$", mcp_type=MCPType.EXCLUDE),
            RouteMap(mcp_type=MCPType.TOOL),
        ],
        # `find_tee_times` signs in and searches with a real browser; GolfNow
        # also measures its booking window first, one search per day.
        httpx_client_kwargs={"timeout": 300},
    )


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    server = create_server()
    if "--http" in argv:
        server.run(transport="http", host="127.0.0.1", port=8001)
    else:
        server.run()


if __name__ == "__main__":
    main()
