"""The bot's cards and buttons as MCP tools, for the assistant.

    python -m tee_time_booker.mcp --cards

The Telegram bot shows tee times as a card with a Book button per slot, asks
Confirm / Cancel before anything pays, and messages the outcome when a run
ends. An agent that talks to the person itself (`apps/assistant`, over its
own Telegram bot) gets the same from these tools: each returns a `telegram`
card (`text`, `format`, `buttons`) to send exactly as given, built by
`tee_time_booker.cards` like the bot's own. A tap comes back to the agent as
the button's data, which goes to `press_button`.

Button data is `tee:<action>:<id>`:

- `tee:book:<offer>:<i>`: a slot on a card. Asks to confirm; books nothing.
- `tee:pay:<payment>`: Confirm. Runs the held call, paying if payment is on.
- `tee:dry:<payment>`: Dry run. Runs the same call without paying.
- `tee:cancel:<payment>`: Cancel.

Only `tee:pay:` can spend money. The tool itself can't tell a tap from an
agent pressing it, so the assistant's PreToolUse hook lets it through only on
the turn that the person's tap of that very button started.

State (searches, cards, held payments) is in memory, as in the bot: a card
from before the server restarted has to be asked for again.
"""

import asyncio
import os
import secrets
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from pydantic import Field

from tee_time_booker.cards import (
    BOOK_SLOT,
    MAX_OFFERED,
    PAYING_TOOLS,
    RUN_TOOLS,
    Found,
    PendingPayment,
    StartedRun,
    TeeTimeOffer,
    ToolResult,
    book_slot,
    course_name,
    describe_payment,
    escape,
    format_offer,
    format_outcome,
    hold_slot,
    make_offer,
    nothing_to_show,
    pick_slots,
    plan_searched,
    remember_found,
    scheduled_status,
    searched,
    shown,
    slot_label,
)

# One person per assistant: every card belongs to the same chat.
CHAT = 0
SEARCH_TOOLS = frozenset({"find_tee_times", "search_tee_times"})
# How often wait_for_run looks at a run.
RUN_POLL_S = 5
PREFIX = "tee:"


def card(text: str, buttons: list[list[dict[str, str]]] | None = None) -> dict:
    """A message for the agent to send as is."""
    out: dict[str, Any] = {"text": text, "format": "rich"}
    if buttons:
        out["buttons"] = buttons
    return out


def _closed(text: str, status: str) -> str:
    """A Confirm card once it's been answered: its buttons go, `status` says
    what became of it (as the bot's `_close_prompt`)."""
    return f"{text}\n\n<footer>{escape(status)}</footer>"


class _RememberSearches(Middleware):
    """Keeps each search's slots, so a card offers only slots really found."""

    def __init__(self, cards: "Cards") -> None:
        self._cards = cards

    async def on_call_tool(self, context: MiddlewareContext, call_next: Any) -> Any:
        result = await call_next(context)
        name = context.message.name
        if name in SEARCH_TOOLS and not result.is_error:
            arguments = dict(context.message.arguments or {})
            await self._cards.remember(name, arguments, _text(result))
        return result


class Cards:
    def __init__(self, server: FastMCP, pays: bool, user_email: str) -> None:
        self._server = server
        self.pays = pays
        self._user_email = user_email
        self._found: Found = {}
        self._offers: dict[str, TeeTimeOffer] = {}
        self._payments: dict[str, PendingPayment] = {}

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            result = await self._server.call_tool(name, arguments)
        except ToolError as e:
            return ToolResult(str(e), is_error=True)
        return ToolResult(_text(result) or "(no content)", is_error=result.is_error)

    async def remember(self, tool: str, arguments: dict[str, Any], text: str) -> None:
        body = ToolResult(text).json()
        if tool == "find_tee_times":
            plan = await self.call("get_plan", {"plan_id": arguments.get("plan_id")})
            if plan.is_error:
                return
            searches, found = plan_searched(plan.json(), body)
        else:
            searches, found = searched(arguments, body)
        remember_found(self._found, CHAT, searches, found)

    # --- cards ------------------------------------------------------------

    async def show(self, arguments: dict[str, Any]) -> dict[str, Any]:
        picked = pick_slots(self._found, CHAT, arguments)
        if not picked.slots:
            raise ToolError(nothing_to_show(picked))
        offer = make_offer(CHAT, picked)
        self._offers = {k: o for k, o in self._offers.items() if not o.expired}
        self._offers[offer.id] = offer
        names: dict[str, str] = {}
        for slot in offer.slots:
            key = slot["course"]
            if key not in names:
                names[key] = slot.get("course_name") or await course_name(
                    self.call, key
                )
        numbered = len(offer.searches) > 1
        buttons = [
            {
                "text": slot_label(slot, i + 1 if numbered else None),
                "data": f"{PREFIX}book:{offer.id}:{i}",
            }
            for i, slot in enumerate(offer.slots)
        ]
        rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
        rows.append([{"text": "None of these", "data": f"{PREFIX}none"}])
        return {
            "telegram": card(format_offer(offer, names), rows),
            "note": shown(offer, picked),
        }

    async def ask(self, payment: PendingPayment) -> dict[str, Any]:
        """The Confirm card for a held payment."""
        self._payments = {k: p for k, p in self._payments.items() if not p.expired}
        self._payments[payment.id] = payment
        runs = payment.tool == BOOK_SLOT or payment.tool in RUN_TOOLS
        if runs and self.pays:
            row = [
                {"text": "Confirm and pay", "data": f"{PREFIX}pay:{payment.id}"},
                {"text": "Dry run", "data": f"{PREFIX}dry:{payment.id}"},
            ]
        elif runs:
            row = [{"text": "Start dry run", "data": f"{PREFIX}dry:{payment.id}"}]
        else:
            # Scheduling pays later, without asking again: no dry run of it.
            row = [{"text": "Confirm", "data": f"{PREFIX}pay:{payment.id}"}]
        row.append({"text": "Cancel", "data": f"{PREFIX}cancel:{payment.id}"})
        text = await describe_payment(self.call, payment, self.pays)
        return {"telegram": card(text, [row]), "payment_id": payment.id}

    async def press(self, data: str) -> dict[str, Any]:
        action, _, rest = data.removeprefix(PREFIX).partition(":")
        if action == "none":
            return {"note": "The person wants none of these; ask what to change."}
        if action == "book":
            offer_id, _, index = rest.partition(":")
            offer = self._offers.get(offer_id)
            slot = None
            if offer is not None and not offer.expired and index.isdigit():
                slot = (
                    offer.slots[int(index)] if int(index) < len(offer.slots) else None
                )
            if slot is None:
                return {"telegram": card("Those times have expired. Ask me again.")}
            return await self.ask(hold_slot(offer, slot))
        if action not in ("pay", "dry", "cancel"):
            raise ToolError(f"Not a tee-time button: {data}")
        payment = self._payments.pop(rest, None)
        if payment is None or payment.expired:
            return {"telegram": card("That button has expired. Ask me again.")}
        text = await describe_payment(self.call, payment, self.pays)
        if action == "cancel":
            return {"telegram": card(_closed(text, "Cancelled. Nothing was paid."))}
        return await self._confirm(payment, text, pay=action == "pay")

    async def _confirm(
        self, payment: PendingPayment, text: str, pay: bool
    ) -> dict[str, Any]:
        """Run a confirmed payment: `pay` False runs it as a dry run."""
        if payment.tool == BOOK_SLOT:
            result = await book_slot(
                self.call, payment.arguments, self._user_email, pay and self.pays
            )
        else:
            arguments = dict(payment.arguments)
            if not pay and payment.tool in RUN_TOOLS:
                arguments["submit_payment"] = False
            result = await self.call(payment.tool, arguments)
        if result.is_error:
            status = f"That didn't work: {result.text[:300]}"
            return {"telegram": card(_closed(text, status))}
        if payment.tool != BOOK_SLOT and payment.tool not in RUN_TOOLS:
            status = scheduled_status(result.json())
            return {"telegram": card(_closed(text, status))}
        run = StartedRun.from_result(result)
        kind = "Dry run" if run.dry_run else "Booking"
        status = f"{kind} started. I'll message you when it finishes."
        return {"telegram": card(_closed(text, status)), "run_id": run.run_id}

    async def wait(self, run_id: str, timeout_s: float) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            got = await self.call("get_run", {"run_id": run_id})
            if got.is_error:
                raise ToolError(got.text)
            run = got.json()
            if run["status"] != "running":
                break
            if loop.time() >= deadline:
                return {"status": "running", "note": "Still running; wait again."}
            await asyncio.sleep(RUN_POLL_S)
        request = await self.call("get_request", {"request_id": run["request_id"]})
        if request.is_error:
            raise ToolError(request.text)
        row = request.json()
        started = StartedRun(
            run_id=run_id,
            course=row["course"],
            target_date=row["target_date"],
            dry_run=run["status"] == "dry_run",
        )
        name = await course_name(self.call, row["course"])
        out: dict[str, Any] = {
            "status": run["status"],
            "telegram": card(format_outcome(started, run, name)),
        }
        if run["status"] == "error":
            out["detail"] = run.get("detail")
        return out


def add_card_tools(server: FastMCP, pays: bool) -> FastMCP:
    cards = Cards(server, pays, os.environ.get("TEE_TIME_USER_EMAIL", ""))
    server.add_middleware(_RememberSearches(cards))

    @server.tool
    async def show_tee_times(
        date: Annotated[str, Field(description="The day searched, YYYY-MM-DD")],
        players: Annotated[int, Field(description="The group size to book")],
        holes: Annotated[int, Field(description="The holes to book")],
        slots: Annotated[
            list[dict[str, str]],
            Field(
                min_length=1,
                max_length=MAX_OFFERED,
                description="The slots to offer, best first: {course: key, "
                "time: 'HH:MM' 24-hour}",
            ),
        ],
    ) -> dict[str, Any]:
        """Tee times from an earlier search_tee_times or find_tee_times, as the
        tee-time bot's card: one row per slot and a Book button each. Send
        `telegram` as given. A tap asks the person to confirm, then books that
        exact slot for the group size and holes it was searched with."""
        return await cards.show(
            {"date": date, "players": players, "holes": holes, "slots": slots}
        )

    @server.tool
    async def ask_to_pay(
        tool: Annotated[
            str,
            Field(description=f"One of {sorted(PAYING_TOOLS)}"),
        ],
        arguments: Annotated[
            dict[str, Any], Field(description="That tool's arguments, as given")
        ],
    ) -> dict[str, Any]:
        """Hold a call that pays (now or later) for the person's Confirm, as the
        bot does: returns the Confirm card to send, with Confirm and pay, Dry
        run and Cancel buttons. The call runs when they tap one."""
        if tool not in PAYING_TOOLS:
            raise ToolError(f"{tool} doesn't pay; call it directly")
        payment = PendingPayment(
            id=secrets.token_urlsafe(8),
            chat_id=CHAT,
            tool=tool,
            arguments=dict(arguments),
        )
        return await cards.ask(payment)

    @server.tool
    async def press_button(
        data: Annotated[str, Field(description="The tapped button's data, tee:…")],
    ) -> dict[str, Any]:
        """Do what a tapped `tee:` button does, as the bot would: a slot asks
        to confirm, Confirm runs the booking (and pays if payment is on), Dry
        run runs it without paying, Cancel drops it. Send `telegram` as given;
        with `run_id`, then call wait_for_run."""
        return await cards.press(data)

    @server.tool
    async def wait_for_run(
        run_id: str,
        timeout_minutes: Annotated[float, Field(gt=0, le=60)] = 15,
    ) -> dict[str, Any]:
        """Wait for a run to finish and return its outcome card (`telegram`),
        worded as the bot words it. `status` running means it's still going
        (a run waiting on a course's release can take over an hour)."""
        return await cards.wait(run_id, timeout_minutes * 60)

    return server


def _text(result: Any) -> str:
    return "\n".join(c.text for c in result.content if hasattr(c, "text"))
