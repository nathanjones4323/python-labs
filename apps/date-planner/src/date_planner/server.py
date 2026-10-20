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
import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from date_planner.config import get_settings
from date_planner.logs import ToolCallLogging, log_crash, setup_logging
from date_planner.tools import register_all

INSTRUCTIONS = """
Data and arithmetic for planning a couple's dates. A date is a day with an
ordered list of stops (a hike, a timed museum entry, a sunset dinner). This
server holds each member's preferences, the couple's past dates, venues and
how each one takes reservations, booking links and windows, sunset, the
weather forecast, travel time between stops, and a timeline checker that
places stops in the order you give and reports what doesn't fit.

This server never books anything and never touches Google. Calendar holds and
Gmail confirmations go through those connectors; the final tap or phone call
is the user's. Read the date-planner skill for the flow that ties them
together.
""".strip()


def create_server(auth: AuthProvider | None = None) -> FastMCP[Any]:
    mcp: FastMCP[Any] = FastMCP(
        name="date-planner", instructions=INSTRUCTIONS, auth=auth
    )
    mcp.add_middleware(ToolCallLogging())

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

    log_file = setup_logging(get_settings())
    log = logging.getLogger("date_planner")
    transport = "http" if args.http else "stdio"
    log.info(
        "server starting (%s) on database %s, logging to %s",
        transport,
        get_settings().database_label(),
        log_file,
    )
    try:
        _serve(args.http)
    except Exception:
        log_crash()
        raise
    finally:
        log.info("server stopped")


def _serve(http: bool) -> None:
    settings = get_settings()
    if not http:
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
