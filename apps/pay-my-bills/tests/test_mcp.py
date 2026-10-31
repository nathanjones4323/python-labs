import asyncio
from dataclasses import replace

import pytest
from conftest import at, sce_bill, venmo_paid, water_bill, water_paid
from fastmcp import Client
from pay_my_bills import mcp as mcp_module
from pay_my_bills import service
from pay_my_bills.db import SplitRequest
from sqlalchemy.exc import OperationalError
from test_pipeline import FakeMailbox, poll, send_all


@pytest.fixture
def server(db, settings, monkeypatch):
    monkeypatch.setattr(mcp_module, "utcnow", lambda: at("2026-10-30 12:00"))
    url = db.kw["bind"].url.render_as_string(hide_password=False)
    return mcp_module.create_server(replace(settings, database_url=url))


def call(server, tool: str, **args):
    async def go():
        async with Client(server) as client:
            return (await client.call_tool(tool, args)).structured_content

    return asyncio.run(go())


def test_only_read_tools(server):
    async def tools():
        async with Client(server) as client:
            return await client.list_tools()

    listed = asyncio.run(tools())
    assert {t.name for t in listed} == {"bill_status", "recent_requests"}
    assert all(t.annotations.read_only_hint for t in listed)


def test_bill_status_lists_what_is_owed_and_unpaid(server, db, settings):
    poll(
        db,
        settings,
        FakeMailbox(water_bill(), water_paid(), sce_bill()),
        "2026-10-15 08:00",
    )

    out = call(server, "bill_status")

    assert out["roommate"] == "Austin Will"
    [request] = out["open_requests"]
    assert request["utility"] == "Ventura Water"
    assert request["share"] == "28.60"
    assert request["total_paid"] == "57.19"
    assert request["state"] == "pending"
    assert "venmo.com" in request["venmo_request_url"]
    [bill] = out["unpaid_bills"]
    assert bill["utility"] == "SCE"


def test_states_follow_the_request(server, db, settings):
    mailbox = FakeMailbox(water_bill(), water_paid())
    poll(db, settings, mailbox, "2026-10-11 08:00")
    send_all(db, settings, "2026-10-11 08:01")
    assert call(server, "bill_status")["open_requests"][0]["state"] == "sent"

    with db() as session:
        request_id = session.query(SplitRequest.id).scalar()
    service.mark(db, request_id, "done", at("2026-10-11 09:00"))
    assert call(server, "bill_status")["open_requests"][0]["state"] == "requested"

    mailbox.add(venmo_paid("28.60"))
    poll(db, settings, mailbox, "2026-10-15 10:00")
    assert call(server, "bill_status")["open_requests"] == []
    [paid] = call(server, "recent_requests")["requests"]
    assert paid["state"] == "paid"
    assert paid["settled_on"] == "2026-10-15"


def test_recent_requests_window(server, db, settings):
    poll(db, settings, FakeMailbox(water_bill(), water_paid()), "2026-10-11 08:00")

    assert len(call(server, "recent_requests", days=30)["requests"]) == 1
    # Paid Oct 12 (or so); a week back from Oct 30 misses it.
    assert call(server, "recent_requests", days=7)["requests"] == []


def test_connections_cannot_write(db, settings):
    url = db.kw["bind"].url.render_as_string(hide_password=False)
    sessions = mcp_module.read_only_sessions(url)
    with pytest.raises(OperationalError, match="readonly"):
        with sessions.begin() as session:
            session.execute(SplitRequest.__table__.delete())
