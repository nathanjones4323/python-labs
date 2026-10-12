"""A local log file, so a long-running server can be debugged after the fact.

Over stdio, stdout *is* the MCP channel and stderr is wherever the client
puts it, so the record that lasts is a rotating file: `logs/date-planner.log`
(relative to where the server starts, like `previews/`), rotated at
`log_max_mb` with `log_backups` kept: 20 MB × 25, about 500 MB, by default.

Every tool call is logged on the way in and out (`ToolCallLogging`), with
its arguments, how long it took, and how it ended: our own errors
(`DatePlannerError`) as a one-line warning, since the caller was told and
can act on them; anything else with the full traceback, since FastMCP hides
those from the client and this file is the only place they're kept.

Several servers can share the file (one per Claude Code session), so each
line carries the process id. Rollover isn't coordinated between them; a
line lost at a rollover is the price of not running a log daemon.
"""

import json
import logging
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import mcp.types as mt
from fastmcp.exceptions import ToolError, ValidationError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult

from date_planner.config import Settings
from date_planner.errors import DatePlannerError

LOG_FILE = "date-planner.log"
FORMAT = "%(asctime)s %(levelname)-7s [%(process)d] %(name)s: %(message)s"
# Long arguments (a busy calendar, a venue's evidence) are cut to this.
MAX_ARGS_CHARS = 2000

# Tile URLs carry the CARTO key in the query string. Headers aren't logged,
# so the Places and ORS keys never reach a record.
_SECRET_PARAM = re.compile(r"([?&](?:key|api_key|token)=)[^&\s\"']+", re.IGNORECASE)

# Chatty at INFO (a request line per map tile, a line per SQL statement);
# DATE_PLANNER_LOG_LEVEL=DEBUG lets httpx's request lines through.
_QUIET = ("httpcore", "sqlalchemy", "asyncio", "PIL")

log = logging.getLogger("date_planner.tools")


class _NotToolCalls(logging.Filter):
    """Drop FastMCP's own record of a failed call: `ToolCallLogging`
    already writes it, with the arguments and the traceback once."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.name.startswith("fastmcp")
            and str(record.msg).startswith(
                ("Error calling tool", "Invalid arguments for tool")
            )
        )


class _Redact(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _SECRET_PARAM.sub(r"\1***", message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def setup_logging(settings: Settings) -> Path:
    """Send this process's logs to the rotating file and return its path."""
    directory = settings.log_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LOG_FILE

    handler = RotatingFileHandler(
        path,
        maxBytes=settings.log_max_mb * 1024 * 1024,
        backupCount=settings.log_backups,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(FORMAT))
    handler.addFilter(_NotToolCalls())
    handler.addFilter(_Redact())

    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    for name in _QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
    if level > logging.DEBUG:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    # FastMCP keeps its logger to itself (propagate=False, a stderr
    # handler); give it the file too.
    logging.getLogger("fastmcp").addHandler(handler)
    return path


def _summarize(arguments: dict[str, Any] | None) -> str:
    text = json.dumps(arguments or {}, default=str, ensure_ascii=False)
    if len(text) > MAX_ARGS_CHARS:
        return f"{text[:MAX_ARGS_CHARS]}… ({len(text)} chars)"
    return text


def _expected(exc: BaseException) -> bool:
    """One of ours, or arguments that didn't validate: the caller was told
    and can fix it, so no traceback."""
    if isinstance(exc, (DatePlannerError, ValidationError)):
        return True
    return isinstance(exc, ToolError) and (
        exc.__cause__ is None or isinstance(exc.__cause__, DatePlannerError)
    )


class ToolCallLogging(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        name = context.message.name
        log.info("→ %s %s", name, _summarize(context.message.arguments))
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            ms = (time.perf_counter() - started) * 1000
            if _expected(exc):
                log.warning("✗ %s %.0fms %s", name, ms, exc)
            else:
                cause = exc.__cause__ or exc
                log.exception(
                    "✗ %s %.0fms unexpected %s", name, ms, type(cause).__name__
                )
            raise
        log.info("← %s %.0fms", name, (time.perf_counter() - started) * 1000)
        return result


def log_crash() -> None:
    """Record whatever is killing the process before it goes."""
    logging.getLogger("date_planner").critical(
        "server exiting on an unhandled error", exc_info=sys.exc_info()
    )
