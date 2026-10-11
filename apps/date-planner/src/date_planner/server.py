"""The MCP server: every tool in one place, over stdio or streamable HTTP.

    date-planner-mcp              # stdio, for Claude Code / Claude Desktop
    date-planner-mcp --http       # streamable HTTP on 127.0.0.1:8002

stdio is the one v1 uses; the HTTP transport is here so the same server can
later be registered as a custom connector. It refuses to start without
DATE_PLANNER_HTTP_TOKEN, so a stray `--http` can't put the database on a
port with no auth in front of it. One static token is the whole auth story:
good enough for a server bound to localhost, not for public hosting.
"""

import argparse
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from date_planner.config import get_settings
from date_planner.tools import register_all

INSTRUCTIONS = """
Data and arithmetic for planning a couple's dates: their preferences and past
dates, restaurants and how each one takes reservations, prefilled booking
links, and when a booking window opens.

This server never books anything and never touches Google. Calendar holds and
Gmail confirmations go through those connectors; the final tap or phone call
is the user's. Read the date-planner skill for the flow that ties them
together.
""".strip()


def create_server(auth: AuthProvider | None = None) -> FastMCP[Any]:
    mcp: FastMCP[Any] = FastMCP(
        name="date-planner", instructions=INSTRUCTIONS, auth=auth
    )

    @mcp.tool
    async def ping() -> str:
        """Check the server is up. Returns "pong"."""
        return "pong"

    register_all(mcp)
    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="date-planner-mcp")
    parser.add_argument(
        "--http",
        action="store_true",
        help="serve streamable HTTP instead of stdio (needs DATE_PLANNER_HTTP_TOKEN)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if not args.http:
        create_server().run()
        return

    if not settings.http_token:
        raise SystemExit(
            "Set DATE_PLANNER_HTTP_TOKEN before serving over HTTP: the tools "
            "read and write the date-planner database."
        )
    server = create_server(
        auth=StaticTokenVerifier(
            tokens={settings.http_token: {"client_id": "date-planner"}}
        )
    )
    server.run(transport="http", host=settings.http_host, port=settings.http_port)


if __name__ == "__main__":
    main()
