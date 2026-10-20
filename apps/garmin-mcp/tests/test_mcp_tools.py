"""The MCP surface, through a real MCP client: which tools exist, their
schemas and annotations, structured results, and errors. One test starts
the actual `garmin-mcp` command over stdio."""

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from garmin_mcp.server import App, create_server
from mcp import Client, StdioServerParameters
from mcp.types import TextContent, Tool

from tests.conftest import strength_spec

READ_TOOLS = {
    "get_account_status",
    "get_recent_activities",
    "get_activity_details",
    "get_training_metrics",
    "search_exercises",
    "list_workouts",
    "get_workout",
    "get_workout_calendar",
    "list_devices",
    "get_device_push_status",
}
WRITE_TOOLS = {
    "create_workout",
    "update_workout",
    "schedule_workout",
    "reschedule_workout",
    "push_workout_to_device",
    "update_activity_exercise_sets",
}


@asynccontextmanager
async def connect(app: App) -> AsyncIterator[Client]:
    # Opened inside each test rather than in a fixture: anyio requires the
    # session to close in the task that opened it.
    async with Client(create_server(app)) as client:
        yield client


async def tools(app: App) -> dict[str, Tool]:
    async with connect(app) as client:
        return {t.name: t for t in (await client.list_tools()).tools}


async def call(app: App, name: str, arguments: dict[str, Any]) -> Any:
    async with connect(app) as client:
        return await client.call_tool(name, arguments)


def error_text(result: Any) -> str:
    """The tool's own message, without the SDK's "Error executing tool X: "."""
    assert result.is_error
    block = result.content[0]
    assert isinstance(block, TextContent)
    if block.text.startswith("Error executing tool "):
        return block.text.split(": ", 1)[1]
    return block.text


async def test_exactly_the_allowlisted_tools(app: App) -> None:
    assert set(await tools(app)) == READ_TOOLS | WRITE_TOOLS


async def test_tools_are_described_and_annotated(app: App) -> None:
    for name, tool in (await tools(app)).items():
        assert tool.description and len(tool.description) > 30, name
        assert tool.output_schema, name
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is (name in READ_TOOLS), name
        props = tool.input_schema.get("properties", {})
        if name in WRITE_TOOLS:
            assert {"dry_run", "confirmation_token"} <= set(props), name
            assert "confirmation_token" not in tool.input_schema.get("required", []), (
                name
            )
        else:
            assert "confirmation_token" not in props, name


async def test_workout_schema_is_typed(app: App) -> None:
    text = str((await tools(app))["create_workout"].input_schema)
    for word in (
        "StrengthWorkout",
        "RunningWorkout",
        "RepeatBlock",
        "PaceTarget",
        "duration_seconds",
    ):
        assert word in text


async def test_structured_results(app: App) -> None:
    result = await call(app, "search_exercises", {"term": "goblet squat", "limit": 3})
    assert not result.is_error
    assert result.structured_content
    hits = result.structured_content["result"]
    assert hits[0]["exercise_id"].startswith("SQUAT/")


async def test_write_tool_previews_over_the_protocol(app: App) -> None:
    async with connect(app) as client:
        result = await client.call_tool("create_workout", {"workout": strength_spec()})
        assert not result.is_error
        data = result.structured_content
        assert data and data["status"] == "approval_required"
        assert data["confirmation_token"]
        assert app.repo.writes == []  # type: ignore[attr-defined]

        confirmed = await client.call_tool(
            "create_workout",
            {
                "workout": strength_spec(),
                "confirmation_token": data["confirmation_token"],
            },
        )
    assert confirmed.structured_content
    assert confirmed.structured_content["status"] == "verified"


async def test_errors_lead_with_their_category(app: App) -> None:
    result = await call(app, "get_workout", {"workout_id": 123})
    assert error_text(result).startswith("not_found: workout:")

    moon = strength_spec(exercises=[{"name": "Moon Press", "sets": 1, "reps": 1}])
    result = await call(app, "create_workout", {"workout": moon})
    assert error_text(result).startswith("not_found: exercise:")


@pytest.mark.parametrize("bad", ["2026-13-01", "next tuesday", "2026-1-5", 20261020])
async def test_malformed_dates_are_rejected(app: App, bad: Any) -> None:
    result = await call(app, "schedule_workout", {"workout_id": 1, "date": bad})
    assert result.is_error


async def test_invalid_workouts_are_rejected_before_anything_runs(app: App) -> None:
    bad = strength_spec(exercises=[{"name": "Plank", "sets": 0, "reps": 1}])
    result = await call(app, "create_workout", {"workout": bad})
    assert "sets" in error_text(result)
    assert app.repo.writes == []  # type: ignore[attr-defined]


async def test_account_status_tool(app: App) -> None:
    result = await call(app, "get_account_status", {})
    assert result.structured_content
    assert result.structured_content["authenticated"] is True
    assert result.structured_content["display_name"] == "runner"


async def test_server_starts_over_stdio(tmp_path: Path) -> None:
    command = Path(sys.executable).parent / "garmin-mcp"
    params = StdioServerParameters(
        command=str(command),
        env={
            "PATH": os.environ.get("PATH", ""),
            "GARMIN_TOKENSTORE": str(tmp_path / "no-tokens"),
            "GARMIN_STATE_DIR": str(tmp_path / "state"),
        },
        cwd=str(tmp_path),
    )
    async with Client(params) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names == READ_TOOLS | WRITE_TOOLS
        status = await client.call_tool("get_account_status", {})
        assert status.structured_content
        assert status.structured_content["authenticated"] is False
        assert status.structured_content["dry_run"] is True
    assert (tmp_path / "state" / "garmin-mcp.log").exists()
