"""The card tools the assistant gets (`tee_time_booker.assistant`), over a
stub of the API's tools so nothing reaches a booking site."""

import asyncio
import json
from typing import Any

import pytest

fastmcp = pytest.importorskip("fastmcp")

from tee_time_booker.assistant import add_card_tools  # noqa: E402

SLOTS = [
    {
        "course": "soule-park",
        "course_name": "Soule Park",
        "start": "2026-10-31T07:10:00",
        "green_fee": 42.0,
        "service_fee": 2.99,
        "side": None,
    },
    {
        "course": "soule-park",
        "course_name": "Soule Park",
        "start": "2026-10-31T07:20:00",
        "green_fee": 42.0,
        "service_fee": 2.99,
        "side": None,
    },
]
REQUEST_ID = "22222222-2222-2222-2222-222222222222"
RUN_ID = "11111111-1111-1111-1111-111111111111"


def _stub(calls: list[tuple[str, dict[str, Any]]], run_status: str = "success"):
    """The API's tools that the cards use, recording each call."""
    server = fastmcp.FastMCP("stub")

    @server.tool
    def search_tee_times(
        user_email: str, date: str, players: int, holes: int
    ) -> dict[str, Any]:
        calls.append(("search_tee_times", {"date": date}))
        return {"tee_times": SLOTS, "errors": []}

    @server.tool
    def get_course(key: str) -> dict[str, Any]:
        return {"key": key, "name": "Soule Park"}

    @server.tool
    def create_request(
        user_email: str,
        course: str,
        target_date: str,
        earliest: str,
        latest: str,
        holes: int,
        players: int,
    ) -> dict[str, Any]:
        calls.append(("create_request", {"course": course, "earliest": earliest}))
        return {"id": REQUEST_ID}

    @server.tool
    def run_request(
        request_id: str, submit_payment: bool, wait_for_release: bool
    ) -> dict[str, Any]:
        calls.append(("run_request", {"submit_payment": submit_payment}))
        return {
            "run_id": RUN_ID,
            "request_id": request_id,
            "course": "soule-park",
            "target_date": "2026-10-31",
            "dry_run": not submit_payment,
            "wait_for_release": wait_for_release,
        }

    @server.tool
    def get_run(run_id: str) -> dict[str, Any]:
        return {
            "id": run_id,
            "request_id": REQUEST_ID,
            "status": run_status,
            "tee_time_local": "2026-10-31T07:10:00",
            "side": None,
            "green_fee": "42.00",
            "confirmation": "ABC123",
            "detail": None,
        }

    @server.tool
    def get_request(request_id: str) -> dict[str, Any]:
        return {"course": "soule-park", "target_date": "2026-10-31"}

    return server


def _data(result) -> dict[str, Any]:
    return json.loads(result.content[0].text)


async def _search_and_show(client) -> dict[str, Any]:
    await client.call_tool(
        "search_tee_times",
        {
            "user_email": "g@example.com",
            "date": "2026-10-31",
            "players": 4,
            "holes": 18,
        },
    )
    shown = await client.call_tool(
        "show_tee_times",
        {
            "date": "2026-10-31",
            "players": 4,
            "holes": 18,
            "slots": [
                {"course": "soule-park", "time": "07:10"},
                {"course": "soule-park", "time": "07:20"},
            ],
        },
    )
    return _data(shown)


def _buttons(card: dict[str, Any]) -> list[dict[str, str]]:
    return [b for row in card["buttons"] for b in row]


def test_a_card_is_the_bots_card_with_book_buttons():
    calls: list = []
    server = add_card_tools(_stub(calls), pays=True)

    async def go():
        async with fastmcp.Client(server) as client:
            return await _search_and_show(client)

    shown = asyncio.run(go())
    card = shown["telegram"]
    assert card["format"] == "rich"
    assert card["text"].startswith("📅 **Soule Park · Sat Oct 31**")
    assert "<b>$42</b>" in card["text"]
    labels = [b["text"] for b in _buttons(card)]
    assert labels == ["7:10 AM", "7:20 AM", "None of these"]
    assert all(b["data"].startswith("tee:") for b in _buttons(card))
    assert all(len(b["data"].encode()) <= 60 for b in _buttons(card))


def test_a_slot_not_searched_is_refused():
    server = add_card_tools(_stub([]), pays=True)

    async def go():
        async with fastmcp.Client(server) as client:
            return await client.call_tool(
                "show_tee_times",
                {
                    "date": "2026-10-31",
                    "players": 4,
                    "holes": 18,
                    "slots": [{"course": "soule-park", "time": "07:10"}],
                },
                raise_on_error=False,
            )

    result = asyncio.run(go())
    assert result.is_error
    assert "search first" in result.content[0].text


@pytest.mark.parametrize(
    ("pays", "press", "submit", "outcome"),
    [
        (True, "pay", True, "✅ **Booked**"),
        (True, "dry", False, "🧪 **Dry run done:**"),
        (False, "dry", False, "🧪 **Dry run done:**"),
    ],
)
def test_book_then_confirm_runs_that_slot_and_reports_it(pays, press, submit, outcome):
    calls: list = []
    status = "success" if submit else "dry_run"
    server = add_card_tools(_stub(calls, run_status=status), pays=pays)

    async def go():
        async with fastmcp.Client(server) as client:
            shown = await _search_and_show(client)
            book = _buttons(shown["telegram"])[0]["data"]
            confirm = _data(await client.call_tool("press_button", {"data": book}))
            chosen = next(
                b["data"]
                for b in _buttons(confirm["telegram"])
                if b["data"].startswith(f"tee:{press}:")
            )
            started = _data(await client.call_tool("press_button", {"data": chosen}))
            done = _data(
                await client.call_tool("wait_for_run", {"run_id": started["run_id"]})
            )
            return confirm, started, done

    confirm, started, done = asyncio.run(go())
    labels = [b["text"] for b in _buttons(confirm["telegram"])]
    if pays:
        assert labels == ["Confirm and pay", "Dry run", "Cancel"]
        assert confirm["telegram"]["text"].startswith("**💳 Book this tee time?**")
    else:
        assert labels == ["Start dry run", "Cancel"]
    assert ("create_request", {"course": "soule-park", "earliest": "07:10:00"}) in calls
    assert ("run_request", {"submit_payment": submit}) in calls
    assert "buttons" not in started["telegram"]
    assert "<footer>" in started["telegram"]["text"]
    assert done["telegram"]["text"].startswith(outcome)


def test_cancel_pays_nothing_and_a_button_works_once():
    calls: list = []
    server = add_card_tools(_stub(calls), pays=True)

    async def go():
        async with fastmcp.Client(server) as client:
            shown = await _search_and_show(client)
            book = _buttons(shown["telegram"])[0]["data"]
            confirm = _data(await client.call_tool("press_button", {"data": book}))
            cancel, pay = (
                next(
                    b["data"]
                    for b in _buttons(confirm["telegram"])
                    if b["data"].startswith(p)
                )
                for p in ("tee:cancel:", "tee:pay:")
            )
            cancelled = _data(await client.call_tool("press_button", {"data": cancel}))
            late = _data(await client.call_tool("press_button", {"data": pay}))
            return cancelled, late

    cancelled, late = asyncio.run(go())
    assert "Nothing was paid" in cancelled["telegram"]["text"]
    assert "expired" in late["telegram"]["text"]
    assert not [c for c in calls if c[0] in ("create_request", "run_request")]


def test_ask_to_pay_only_holds_paying_tools():
    server = add_card_tools(_stub([]), pays=True)

    async def go():
        async with fastmcp.Client(server) as client:
            return await client.call_tool(
                "ask_to_pay",
                {"tool": "list_plans", "arguments": {}},
                raise_on_error=False,
            )

    assert asyncio.run(go()).is_error


def test_the_real_server_gets_the_card_tools(seeded, db, tmp_path):
    from tee_time_booker.api import create_app
    from tee_time_booker.config import Settings
    from tee_time_booker.mcp import create_server

    settings = Settings(
        database_url=None,
        plan_id=None,
        submit_payment=False,
        headless=True,
        artifacts_dir=tmp_path,
        target_date_override=None,
    )
    app = create_app(settings, sessions=db)
    server = add_card_tools(create_server(app), pays=app.state.worker.pays(True))

    async def go():
        async with fastmcp.Client(server) as client:
            names = {t.name for t in await client.list_tools()}
            plans = await client.call_tool("list_plans", {})
            return names, plans

    names, plans = asyncio.run(go())
    assert {"show_tee_times", "press_button", "ask_to_pay", "wait_for_run"} <= names
    assert {"list_plans", "search_tee_times", "run_request"} <= names
    assert not plans.is_error
