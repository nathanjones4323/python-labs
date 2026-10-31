"""The app's records as read-only MCP tools, for the assistant.

    pay-my-bills-mcp          # stdio

It only reads the bot's database: what's been billed and paid, and how far
asking the roommate for each share got. Nothing here polls Gmail, sends a
message or marks a request; the bot (`pay-my-bills`) does all of that, and
its buttons stay the only way to change a request. Every connection is
`query_only`, so a write fails rather than lands.

Run it from `apps/pay-my-bills` so its `.env` (the roommate, and the
database URL if it isn't the default) is the one loaded.
"""

from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from pay_my_bills import messages, pipeline
from pay_my_bills.config import Settings
from pay_my_bills.db import (
    UTILITY_NAMES,
    Bill,
    Payment,
    SplitRequest,
    make_engine,
    utcnow,
)
from pay_my_bills.split import dollars

INSTRUCTIONS = """
The person's shared bills with their roommate (Ventura Water, SCE, rent), as
the pay-my-bills bot recorded them from Gmail. Read-only: the bot sends each
Venmo request and its buttons mark it requested, paid or skipped.

- `bill_status`: what the roommate still owes (open requests) and bills not
  paid yet. The same as the bot's /status.
- `recent_requests`: every share asked for lately, settled or not, for
  questions like "did Austin pay the water bill?".

A request's `state` is one of: `pending` (the bot hasn't sent it yet),
`sent` (sent to the person, not requested in Venmo yet), `requested`
(requested in Venmo, waiting on the roommate), `paid`, `skipped`. Money is
dollars as strings ("28.60"); dates are local YYYY-MM-DD.
"""


def _state(request: SplitRequest) -> str:
    if request.settled_at:
        return "paid"
    if request.skipped_at:
        return "skipped"
    if request.requested_at:
        return "requested"
    if request.notified_at:
        return "sent"
    return "pending"


def _day(value: datetime | None, settings: Settings) -> str | None:
    return value.astimezone(settings.timezone).date().isoformat() if value else None


def request_dict(request: SplitRequest, settings: Settings) -> dict[str, Any]:
    payment = request.payment
    return {
        "id": request.id,
        "utility": UTILITY_NAMES[payment.utility],
        "note": request.note,
        "share": dollars(request.share_cents),
        "total_paid": dollars(payment.total_cents),
        "fee": dollars(payment.fee_cents),
        "paid_on": payment.paid_on.isoformat(),
        # "assumed": SCE autopay expected that day, no confirmation email.
        "payment_source": payment.source,
        "state": _state(request),
        "sent_on": _day(request.notified_at, settings),
        "requested_on": _day(request.requested_at, settings),
        "settled_on": _day(request.settled_at, settings),
        "skipped_on": _day(request.skipped_at, settings),
        "venmo_request_url": messages.venmo_url(request, settings),
    }


def bill_dict(bill: Bill, settings: Settings) -> dict[str, Any]:
    expected = pipeline.autopay_date(bill, settings)
    return {
        "id": bill.id,
        "utility": UTILITY_NAMES[bill.utility],
        "statement_date": bill.statement_date.isoformat(),
        "due_date": bill.due_date.isoformat() if bill.due_date else None,
        "amount_due": dollars(bill.amount_due_cents),
        "autopay_expected_on": expected.isoformat() if expected else None,
    }


def read_only_sessions(url: str) -> sessionmaker[Session]:
    engine = make_engine(url)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _query_only(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA query_only=ON")
            cursor.close()

    return sessionmaker(engine, expire_on_commit=False)


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    # No `migrate`: the bot owns the schema and is the only writer.
    sessions = read_only_sessions(settings.database_url)
    mcp = FastMCP(name="pay-my-bills", instructions=INSTRUCTIONS)

    @mcp.tool(annotations={"readOnlyHint": True})
    def bill_status() -> dict[str, Any]:
        """What the roommate still owes, and bills not paid yet."""
        with sessions() as session:
            return {
                "roommate": settings.roommate_name,
                "open_requests": [
                    request_dict(r, settings) for r in pipeline.open_requests(session)
                ],
                "unpaid_bills": [
                    bill_dict(b, settings) for b in pipeline.unpaid_bills(session)
                ],
            }

    @mcp.tool(annotations={"readOnlyHint": True})
    def recent_requests(
        days: Annotated[
            int, Field(ge=1, le=730, description="How far back, by payment date")
        ] = 90,
    ) -> dict[str, Any]:
        """Every share asked of the roommate for payments in the last `days`,
        newest first, whatever its state."""
        since: date = pipeline.local_today(settings, utcnow()) - timedelta(days=days)
        with sessions() as session:
            requests = session.scalars(
                select(SplitRequest)
                .join(SplitRequest.payment)
                .where(Payment.paid_on >= since)
                .order_by(Payment.paid_on.desc(), SplitRequest.id.desc())
            )
            return {
                "roommate": settings.roommate_name,
                "since": since.isoformat(),
                "requests": [request_dict(r, settings) for r in requests],
            }

    return mcp


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
