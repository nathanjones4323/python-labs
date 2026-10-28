import asyncio
from dataclasses import replace
from datetime import date, timedelta

import pytest
from campsite_booker import booker
from campsite_booker import mcp as mcp_module
from campsite_booker.checkout import CheckoutResult
from campsite_booker.db import DRY_RUN, HELD, PAID
from conftest import site
from fastmcp import Client
from fastmcp.exceptions import ToolError

ARRIVE = date.today() + timedelta(days=10)


@pytest.fixture
def server(settings, recgov, tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "RecGov", lambda: recgov)
    recgov.close = lambda: None
    settings = replace(settings, database_url=f"sqlite:///{tmp_path / 'mcp.db'}")
    return mcp_module.create_server(settings)


def call(server, tool: str, **args):
    async def go():
        async with Client(server) as client:
            return (await client.call_tool(tool, args)).structured_content

    return asyncio.run(go())


def test_tools_are_the_ones_the_assistant_allowlists(server):
    async def names():
        async with Client(server) as client:
            return {t.name for t in await client.list_tools()}

    assert asyncio.run(names()) == {
        "search_campgrounds",
        "find_open_campsites",
        "create_watch",
        "list_watches",
        "stop_watch",
        "resume_watch",
        "hold_campsite",
        "pay_hold",
        "set_watch_auto_pay",
        "list_holds",
        "alert_timing",
    }


def test_find_open_campsites_by_id(server, recgov):
    recgov.sites["1"] = [
        site("10", [ARRIVE, ARRIVE + timedelta(days=1)], site="010"),
        site("11", [ARRIVE]),
    ]
    out = call(
        server,
        "find_open_campsites",
        arrival_from=ARRIVE.isoformat(),
        nights=2,
        campground_ids=["1", "2"],
    )
    assert [c["campground_id"] for c in out["open"]] == ["1"]
    stay = out["open"][0]["stays"][0]
    assert (stay["site"], stay["check_in"], stay["nights"]) == (
        "010",
        ARRIVE.isoformat(),
        2,
    )
    assert stay["url"].endswith("/camping/campsites/10")
    assert out["nothing_open"] == ["Campground 2"]


def test_watches_round_trip(server):
    made = call(
        server,
        "create_watch",
        campground_id="1",
        arrival_from=ARRIVE.isoformat(),
        arrival_to=(ARRIVE + timedelta(days=14)).isoformat(),
        nights=2,
        arrival_weekdays=["Fri"],
        auto_hold=True,
    )
    assert made["arrival_weekdays"] == ["Fri"]
    assert made["auto_hold"] is True and made["active"] is True
    listed = call(server, "list_watches")["result"]
    assert [w["watch_id"] for w in listed] == [made["watch_id"]]
    call(server, "stop_watch", watch_id=made["watch_id"])
    assert call(server, "list_watches")["result"] == []


def test_bad_input_is_a_readable_error(server):
    with pytest.raises(ToolError, match="weekday"):
        call(
            server,
            "create_watch",
            campground_id="1",
            arrival_from=ARRIVE.isoformat(),
            nights=2,
            arrival_weekdays=["Funday"],
        )
    with pytest.raises(ToolError, match="NotFound"):
        call(server, "stop_watch", watch_id=999)


def test_create_watch_says_when_unreleased_dates_go_on_sale(server, recgov):
    soon = ARRIVE + timedelta(days=5)
    release_day = (date.today() + timedelta(days=3)).isoformat()
    recgov.release_payloads["1"] = {
        "current_release": {"end": f"{ARRIVE.isoformat()}T00:00:00Z"},
        "next_release": {
            "release_time": f"{release_day}T10:00:00-04:00",
            "end": f"{(ARRIVE + timedelta(days=30)).isoformat()}T00:00:00Z",
        },
    }
    made = call(
        server,
        "create_watch",
        campground_id="1",
        arrival_from=soon.isoformat(),
        nights=2,
    )
    assert made["on_sale_through"] == ARRIVE.isoformat()
    assert made["on_sale_at"].startswith(release_day)
    on_sale = call(
        server,
        "create_watch",
        campground_id="1",
        arrival_from=ARRIVE.isoformat(),
        nights=2,
    )
    assert "on_sale_at" not in on_sale


def test_auto_pay_and_paying_holds(server, recgov, monkeypatch):
    made = call(
        server,
        "create_watch",
        campground_id="1",
        arrival_from=ARRIVE.isoformat(),
        nights=2,
    )
    on = call(server, "set_watch_auto_pay", watch_id=made["watch_id"])
    assert (on["auto_pay"], on["auto_hold"], on["payment_enabled"]) == (
        True,
        True,
        False,
    )

    recgov.sites["1"] = [site("10", [ARRIVE, ARRIVE + timedelta(days=1)])]
    asked = []

    def hold(settings, stay, pay_group_size):
        asked.append(pay_group_size)
        return booker.HoldResult(
            HELD,
            "In your cart",
            "https://www.recreation.gov/camping/reservations/orderdetails?id=9",
            checkout=CheckoutResult(DRY_RUN, "Dry run"),
        )

    monkeypatch.setattr(booker, "hold", hold)
    held = call(
        server,
        "hold_campsite",
        campground_id="1",
        campsite_id="10",
        check_in=ARRIVE.isoformat(),
        nights=2,
        pay=True,
        group_size=3,
    )
    assert asked == [3]
    assert (held["status"], held["checkout_status"]) == (HELD, DRY_RUN)
    assert held["pay_at"].endswith("/cart")

    monkeypatch.setattr(
        booker,
        "pay",
        lambda s, url, size, label: (CheckoutResult(PAID, "Paid", "77"), None),
    )
    paid = call(server, "pay_hold", hold_id=held["hold_id"])
    assert (paid["checkout_status"], paid["confirmation"], paid["pay_at"]) == (
        PAID,
        "77",
        None,
    )
