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
    monkeypatch.setattr(mcp_module, "Campgrounds", lambda: recgov)
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
        "ask_auto_pay",
        "press_button",
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
        lambda s, url, size, label, expect: (CheckoutResult(PAID, "Paid", "77"), None),
    )
    paid = call(server, "pay_hold", hold_id=held["hold_id"])
    assert (paid["checkout_status"], paid["confirmation"], paid["pay_at"]) == (
        PAID,
        "77",
        None,
    )


def test_state_parks_are_searched_and_watched_but_not_held(server, recgov):
    big_sur = "rc-1001-2001"
    recgov.sites[big_sur] = [site("50001", [ARRIVE])]
    found = call(server, "search_campgrounds", query="big sur")["result"]
    assert [(c["campground_id"], c["source"]) for c in found] == [
        (big_sur, "reservecalifornia")
    ]
    assert found[0]["url"] == "https://www.reservecalifornia.com/park/1001/2001"

    made = call(
        server,
        "create_watch",
        campground_id=big_sur,
        arrival_from=ARRIVE.isoformat(),
        nights=1,
    )
    assert made["campground_id"] == big_sur
    # No release schedule to report, and none asked for.
    assert "on_sale_through" not in made and "note" not in made
    assert recgov.release_calls == []

    with pytest.raises(ToolError, match="ReserveCalifornia"):
        call(
            server,
            "create_watch",
            campground_id=big_sur,
            arrival_from=ARRIVE.isoformat(),
            nights=1,
            auto_hold=True,
        )
    with pytest.raises(ToolError, match="ReserveCalifornia"):
        call(
            server,
            "hold_campsite",
            campground_id=big_sur,
            campsite_id="50001",
            check_in=ARRIVE.isoformat(),
            nights=1,
        )


def _buttons(card):
    return [b for row in card["buttons"] for b in row]


def test_search_hold_and_pay_by_the_bots_cards(server, recgov, monkeypatch):
    """The assistant's path: a search card with Hold buttons, a tap holds,
    the hold card's Pay button pays, each card the watcher bot's own."""
    recgov.sites["1"] = [site("10", [ARRIVE, ARRIVE + timedelta(days=1)], site="010")]
    out = call(
        server,
        "find_open_campsites",
        arrival_from=ARRIVE.isoformat(),
        nights=2,
        campground_ids=["1"],
    )
    [found] = out["telegram"]
    assert found["format"] == "html"
    assert found["text"].startswith("🏕 <b>Open at Campground 1</b>")
    assert "Site 010" in found["text"]
    hold_button = next(b for b in _buttons(found) if "data" in b)
    assert hold_button["text"] == "🤖 Hold 010"
    assert hold_button["data"].startswith("camp:hold:")
    # Holding one site leaves the card's other Hold buttons, as in the bot.
    assert hold_button["keep"] is True
    assert any(b.get("url", "").endswith("/campsites/10") for b in _buttons(found))
    # No watch found these, so there's nothing to stop.
    assert not any(b.get("data", "").startswith("camp:stop:") for b in _buttons(found))

    monkeypatch.setattr(
        booker,
        "hold",
        lambda settings, stay, pay_group_size: booker.HoldResult(
            HELD, "In your cart", "https://www.recreation.gov/x"
        ),
    )
    held = call(server, "press_button", data=hold_button["data"])
    assert held["status"] == HELD
    assert "In your cart" in held["telegram"]["text"]
    pay_button = next(
        b
        for b in _buttons(held["telegram"])
        if b.get("data", "").startswith("camp:pay:")
    )
    assert pay_button["text"] == "💳 Pay (dry run)"

    again = call(server, "press_button", data=hold_button["data"])
    assert again["telegram"]["text"].startswith("Already done")

    monkeypatch.setattr(
        booker,
        "pay",
        lambda s, url, size, label, expect: (CheckoutResult(PAID, "Paid", "77"), None),
    )
    paid = call(server, "press_button", data=pay_button["data"])
    assert paid["checkout_status"] == PAID
    assert "Booked and paid" in paid["telegram"]["text"]


def test_only_the_latest_search_stays_are_kept(server, recgov, monkeypatch):
    monkeypatch.setattr(mcp_module, "FOUND_MAX", 1)
    recgov.sites["1"] = [
        site("10", [ARRIVE, ARRIVE + timedelta(days=1)], site="010"),
        site("11", [ARRIVE, ARRIVE + timedelta(days=1)], site="011"),
    ]
    [found] = call(
        server,
        "find_open_campsites",
        arrival_from=ARRIVE.isoformat(),
        nights=2,
        campground_ids=["1"],
    )["telegram"]
    first, _ = [b["data"] for b in _buttons(found) if "data" in b]
    gone = call(server, "press_button", data=first)
    assert gone["telegram"]["text"] == "That opening is no longer on record."


def test_auto_pay_is_a_card_then_a_tap(server):
    made = call(
        server,
        "create_watch",
        campground_id="1",
        arrival_from=ARRIVE.isoformat(),
        nights=2,
    )
    asked = call(server, "ask_auto_pay", watch_id=made["watch_id"])
    data = [b["data"] for b in _buttons(asked["telegram"])]
    assert data == [f"camp:autopay:{made['watch_id']}", "camp:none"]
    on = call(server, "press_button", data=data[0])
    assert (on["auto_pay"], on["auto_hold"]) == (True, True)
    stopped = call(server, "press_button", data=f"camp:booked:{made['watch_id']}")
    assert stopped["active"] is False
