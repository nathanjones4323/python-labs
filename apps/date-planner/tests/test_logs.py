"""The log file: what a tool call leaves behind, and what it mustn't."""

import logging
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import pytest
from date_planner.config import Settings
from date_planner.errors import NotFound
from date_planner.logs import setup_logging
from date_planner.server import create_server
from date_planner.tools import as_tool_error
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError


def server() -> FastMCP[Any]:
    mcp = create_server()

    @mcp.tool
    @as_tool_error
    async def missing() -> str:
        raise NotFound("No couple 42")

    @mcp.tool
    async def broken() -> str:
        raise ValueError("kaboom")

    return mcp


async def call(tool: str, **arguments: Any) -> None:
    async with Client(server()) as connected:
        await connected.call_tool(tool, arguments)


def tool_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "date_planner.tools"]


async def test_a_call_is_logged_in_and_out(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    await call("ping")
    first, last = tool_records(caplog)
    assert first.getMessage() == "→ ping {}"
    assert last.getMessage().startswith("← ping ")


async def test_our_own_errors_are_one_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    with pytest.raises(ToolError):
        await call("missing")
    failed = tool_records(caplog)[-1]
    assert failed.levelno == logging.WARNING
    assert "NotFound: No couple 42" in failed.getMessage()
    assert failed.exc_info is None


async def test_bad_arguments_are_one_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    with pytest.raises(ToolError):
        await call("build_booking_link", stop_id="nope")
    failed = tool_records(caplog)[-1]
    assert failed.levelno == logging.WARNING
    assert failed.exc_info is None


async def test_anything_else_keeps_its_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with pytest.raises(ToolError):
        await call("broken")
    failed = tool_records(caplog)[-1]
    assert failed.levelno == logging.ERROR
    assert "unexpected ValueError" in failed.getMessage()
    assert failed.exc_info is not None


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Undo `setup_logging`, which configures the process's loggers."""
    root, fastmcp = logging.getLogger(), logging.getLogger("fastmcp")
    before = (root.level, list(root.handlers), list(fastmcp.handlers))
    yield
    for handler in set(root.handlers) - set(before[1]):
        handler.close()
    root.setLevel(before[0])
    root.handlers[:] = before[1]
    fastmcp.handlers[:] = before[2]


@pytest.fixture
def log_file(tmp_path: Path, restore_logging: None) -> Path:
    return setup_logging(Settings(log_dir=tmp_path / "logs"))


async def test_the_file_gets_one_traceback_per_failure(log_file: Path) -> None:
    with pytest.raises(ToolError):
        await call("broken")
    text = log_file.read_text()
    assert "→ broken {}" in text
    assert "ValueError: kaboom" in text
    # FastMCP's own "Error calling tool" record would repeat it.
    assert "fastmcp.server" not in text
    assert text.count("ValueError: kaboom") == 1


def test_keys_in_urls_are_masked(log_file: Path) -> None:
    logging.getLogger("httpx").warning(
        "GET https://basemaps.cartocdn.com/1/2/3@2x.png?key=s3cret&x=1"
    )
    text = log_file.read_text()
    assert "s3cret" not in text
    assert "?key=***&x=1" in text


@pytest.mark.usefixtures("restore_logging")
def test_rotation_keeps_what_the_settings_say(tmp_path: Path) -> None:
    before = list(logging.getLogger().handlers)
    setup_logging(Settings(log_dir=tmp_path, log_max_mb=7, log_backups=3))
    (handler,) = set(logging.getLogger().handlers) - set(before)
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == 7 * 1024 * 1024
    assert handler.backupCount == 3
