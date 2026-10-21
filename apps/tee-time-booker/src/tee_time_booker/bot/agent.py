"""The conversation: a message in, the model and the booking tools, a reply out.

A manual tool-use loop rather than the SDK's tool runner, because one kind of
call must not run at all: a paying booking (`tools.asks_to_pay`). The loop
answers that call with "held for confirmation" and hands it back to the bot
as a `PendingPayment`; the bot shows Confirm / Cancel and runs it only on
Confirm. The model can ask to pay; it can't pay.

Each chat's history only ever grows: whole responses are appended as they
came back, and what happens outside the loop (a confirmed payment) is added
as a note rather than by editing earlier turns. `/new` starts over.
"""

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from anthropic.types import Message, MessageParam, ToolResultBlockParam, ToolUseBlock
from loguru import logger

from tee_time_booker.bot.llm import Model, ModelError
from tee_time_booker.bot.tools import (
    RUN_TOOLS,
    BookingTools,
    ToolResult,
    asks_to_pay,
)
from tee_time_booker.config import BotSettings

# Tool-use rounds per message before giving up on a runaway loop.
MAX_STEPS = 12
# A Confirm button older than this does nothing; ask again.
PAYMENT_TTL_S = 15 * 60

SYSTEM = """\
You book golf tee times for {email} over Telegram, using the tee-time-booker tools.

Booking now:
- search_tee_times looks for what's open on a day across every course at once \
(user_email {email}, date, players, holes, and an earliest-latest tee-off window), \
without booking. Search every course unless the person names one or more: then pass \
just those keys in courses (list_courses has the keys). For a time like "around 9", \
search an hour either side; for "morning" or "after 2", the obvious window. A plan's \
own course and group can also be searched with find_tee_times.
- To offer tee times, call show_tee_times with the ones that best fit what the person \
asked for (at most twelve, best first: closest to the time they asked, spread over \
a few courses rather than every slot at one) rather than listing them in your reply. \
It shows them one card with a Book button per slot; a tap asks them to confirm and \
then books exactly that slot, so you don't book it yourself. After it, reply with one \
short line or nothing at all; mention courses that couldn't be searched only if the \
person asked about them.
- Before booking, check what's open unless the person says not to.

Booking ahead:
- A plan is a standing wish: a course, the Nth upcoming weekday, a tee-off window, a \
group size and holes. list_plans shows each plan with the date it would book next.
- auto_book hands a booking to the scheduler, which books it as soon as the day can \
be booked (a course that releases days at a fixed time is tried right at the \
release; others each morning once the day is near), and messages the person how it \
went. create_plan with auto_book books each next date every week; create_request \
with auto_book books one date once. set_plan_auto_book turns a plan's on or off; \
cancel_request calls off a scheduled one-off. first_attempt_at in the result says \
when it starts trying.
- Without auto_book, a plan is only booked when asked (book_plan), and a one-off \
request only when run (run_request).

How runs work:
- book_plan and run_request start a run in the background and answer at once with a \
run id. The bot itself tells the person the run started and messages them when it \
ends, so don't repeat either, and don't poll get_run unless they ask how a run is going.
- When a run fails, you're told in a bracketed note and the person hears nothing \
until you reply. Say what went wrong in plain words, never the raw error, then ask \
a short follow-up about what they'd like instead. The scheduler won't try that \
date again, but a plan's later weeks are unaffected.
- When a plan's run found no tee times in its window, ask whether they'd like a \
different time on that date while the weekly schedule stays as it is. Search the \
day first, and if anything is open, offer the closest times with show_tee_times \
along with the question. If they then name a time, book that date alone \
(create_request, then run_request) and leave the plan as it is: create_plan with \
the plan's name would change every week.
- A run takes the best slot inside the window; it can't be aimed at one particular \
slot. To get a particular time, give a narrow window around it.
- Plans and requests need user_email {email}. env_prefix picks the site account; \
leave it out and the person's only account on that course's site is used.

Paying:
- submit_payment=false is a dry run: it stops at checkout and pays nothing. Set \
submit_payment=true only when the person has clearly asked to book for real.
- auto_book=true (and set_plan_auto_book enabled=true) pays later without asking \
again. Set it only when the person has asked to have it booked automatically.
- A call that pays, now or later, is held: the person gets a Confirm / Cancel button \
and nothing happens until they tap Confirm. Don't repeat the call; point them at the \
button.
- {payment_switch}

People speak in their own terms ("this Saturday", "after 9", "every Sunday"). Work \
out the actual date or weekday from today's date below.

Replies are read on a phone, so keep them as short as a text message: one to three \
lines, no preamble or sign-off. Don't explain how the tools work, restate the \
question, or offer next steps unless asked. Telegram renders Markdown: put the key \
fact in **bold** (the date, the time picked), use no headings, and leave a blank \
line between lines (a single newline doesn't break the line). If you mention tee \
times in text, give at most three, like "**Sat Oct 24:** 8:44 AM, 9:02 AM ($20.75)". \
Write times like 9:12 AM and dates like Sat Oct 24.
Today is {today:%A %B %-d, %Y}."""

# Answered by the bot itself, not the MCP server: the slots must come from a
# search in the same chat, so the card can't show a made-up one.
SHOW_TEE_TIMES = "show_tee_times"
SEARCH_TOOLS = frozenset({"find_tee_times", "search_tee_times"})
MAX_OFFERED = 12
SHOW_TEE_TIMES_DEF = {
    "name": SHOW_TEE_TIMES,
    "description": (
        "Show the person tee times from an earlier search_tee_times or "
        "find_tee_times result as one card with a Book button per slot, from any "
        "of the courses searched. A tap asks them to confirm, then books that "
        "exact slot."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "format": "date",
                "description": "The day searched, YYYY-MM-DD",
            },
            "slots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "course": {"type": "string", "description": "Course key"},
                        "time": {
                            "type": "string",
                            "description": "Tee-off, HH:MM 24-hour",
                        },
                    },
                    "required": ["course", "time"],
                },
                "minItems": 1,
                "maxItems": MAX_OFFERED,
                "description": "The slots to offer, best first",
            },
        },
        "required": ["date", "slots"],
    },
}
# A held booking of one slot from a card, run by `confirm` (not an MCP tool).
BOOK_SLOT = "book_slot"

PAYMENT_ON = "Payment is switched on for this install: a confirmed booking really pays."
PAYMENT_OFF = (
    "Payment is switched off for this install (TEE_TIME_SUBMIT_PAYMENT), so every run "
    "is a dry run whatever you ask. Say so if the person wants to book for real."
)


@dataclass(frozen=True)
class PendingPayment:
    """A paying call the model made, waiting on the person's Confirm."""

    id: str
    chat_id: int
    tool: str
    arguments: dict[str, Any]
    created: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created > PAYMENT_TTL_S


@dataclass(frozen=True)
class StartedRun:
    """A run the bot should watch: the parts of the API's `RunStarted` it reports."""

    run_id: str
    course: str
    target_date: str
    dry_run: bool

    @classmethod
    def from_result(cls, result: ToolResult) -> "StartedRun":
        body = result.json()
        return cls(
            run_id=body["run_id"],
            course=body["course"],
            target_date=body["target_date"],
            dry_run=body["dry_run"],
        )


@dataclass(frozen=True)
class TeeTimeOffer:
    """Slots shown on a card, each with a Book button. `slots` are rows of a
    search result, as the API returned them, each with its `course` key;
    `searches` says, per course, who searched for how many (`user_email`,
    `course`, `env_prefix`, `holes`, `players`)."""

    id: str
    chat_id: int
    searches: dict[str, dict[str, Any]]
    slots: tuple[dict[str, Any], ...]
    created: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created > PAYMENT_TTL_S


@dataclass
class Reply:
    text: str
    payments: list[PendingPayment] = field(default_factory=list)
    runs: list[StartedRun] = field(default_factory=list)
    offers: list[TeeTimeOffer] = field(default_factory=list)
    # The model couldn't be reached; `text` says so.
    failed: bool = False


class BookingAgent:
    def __init__(
        self,
        client: Model,
        tools: BookingTools,
        settings: BotSettings,
        *,
        pays: bool,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._client = client
        self._tools = tools
        self._settings = settings
        self._pays = pays
        self._today = today
        self._tool_defs: list | None = None
        self._history: dict[int, list[MessageParam]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._payments: dict[str, PendingPayment] = {}
        self._offers: dict[str, TeeTimeOffer] = {}
        # The latest search per (chat, course, day), for show_tee_times: the
        # search itself and the slots it found.
        self._found: dict[
            tuple[int, str, str], tuple[dict[str, Any], list[dict[str, Any]]]
        ] = {}

    def _lock(self, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault(chat_id, asyncio.Lock())

    def _system(self) -> str:
        return SYSTEM.format(
            email=self._settings.user_email,
            payment_switch=PAYMENT_ON if self._pays else PAYMENT_OFF,
            today=self._today(),
        )

    async def respond(
        self,
        chat_id: int,
        text: str,
        on_step: Callable[[list[ToolUseBlock]], Awaitable[None]] | None = None,
    ) -> Reply:
        """Answer one message from the person, running tools as the model asks.

        `on_step` hears each round of tool calls before it runs (the bot shows
        it as progress)."""
        async with self._lock(chat_id):
            if self._tool_defs is None:
                self._tool_defs = [
                    *await self._tools.definitions(),
                    SHOW_TEE_TIMES_DEF,
                ]
            history = self._history.setdefault(chat_id, [])
            # Built up here and kept only if the turn completes, so a failed
            # request never leaves a tool call without its result in history.
            turn: list[MessageParam] = [{"role": "user", "content": text}]
            reply = Reply(text="")
            try:
                for _ in range(MAX_STEPS):
                    response = await self._client.messages.create(
                        model=self._settings.model,
                        max_tokens=16000,
                        system=self._system(),
                        tools=self._tool_defs,
                        messages=[*history, *turn],
                        output_config={"effort": self._settings.effort},
                        cache_control={"type": "ephemeral"},
                    )
                    turn.append({"role": "assistant", "content": response.content})
                    if response.stop_reason != "tool_use":
                        # After a card, saying nothing more is fine.
                        reply.text = _text(response.content) or (
                            "" if reply.offers else _stop_text(response)
                        )
                        break
                    calls = [b for b in response.content if b.type == "tool_use"]
                    if on_step is not None:
                        await on_step(calls)
                    results = await asyncio.gather(
                        *(self._run_tool(chat_id, call, reply) for call in calls)
                    )
                    turn.append({"role": "user", "content": list(results)})
                else:
                    reply.text = "That took too many steps; try asking more simply."
            except ModelError as e:
                logger.exception("Model request failed")
                reply.text = f"Couldn't reach the model ({e}). Try again."
                reply.failed = True
                return reply
            history.extend(turn)
            return reply

    async def _run_tool(
        self, chat_id: int, call: ToolUseBlock, reply: Reply
    ) -> ToolResultBlockParam:
        arguments = dict(call.input)
        if call.name == SHOW_TEE_TIMES:
            return self._show(chat_id, call, reply)
        if self._pays and asks_to_pay(call.name, arguments):
            payment = PendingPayment(
                id=secrets.token_urlsafe(8),
                chat_id=chat_id,
                tool=call.name,
                arguments=arguments,
            )
            # Untapped buttons would otherwise stay in memory for good.
            for stale in [p.id for p in self._payments.values() if p.expired]:
                del self._payments[stale]
            self._payments[payment.id] = payment
            reply.payments.append(payment)
            content = (
                "Held for the person's confirmation: they now have a Confirm / Cancel "
                "button. Nothing has been booked or paid yet."
            )
            return {"type": "tool_result", "tool_use_id": call.id, "content": content}
        logger.info(f"Tool {call.name} {arguments}")
        result = await self._tools.call(call.name, arguments)
        if call.name in RUN_TOOLS and not result.is_error:
            reply.runs.append(StartedRun.from_result(result))
        if call.name in SEARCH_TOOLS and not result.is_error:
            await self._remember_found(chat_id, call.name, arguments, result)
        return {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": result.text,
            "is_error": result.is_error,
        }

    async def _remember_found(
        self, chat_id: int, tool: str, arguments: dict[str, Any], result: ToolResult
    ) -> None:
        if tool == "find_tee_times":
            # A plan's search: the plan says where and for how many.
            plan = await self._tools.call(
                "get_plan", {"plan_id": arguments.get("plan_id")}
            )
            if plan.is_error:
                return
            row = plan.json()
            fields = ("user_email", "course", "env_prefix", "holes", "players")
            searches = {row["course"]: {key: row[key] for key in fields}}
            found = [{**slot, "course": row["course"]} for slot in result.json()]
        else:
            found = result.json()["tee_times"]
            searches = {
                slot["course"]: {
                    "user_email": arguments.get("user_email"),
                    "course": slot["course"],
                    "env_prefix": None,
                    "holes": arguments.get("holes"),
                    "players": arguments.get("players"),
                }
                for slot in found
            }
        by_place: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for slot in found:
            by_place.setdefault((slot["course"], slot["start"][:10]), []).append(slot)
        for (course, day), slots in by_place.items():
            self._found[(chat_id, course, day)] = (searches[course], slots)

    def _show(
        self, chat_id: int, call: ToolUseBlock, reply: Reply
    ) -> ToolResultBlockParam:
        arguments = dict(call.input)
        day = str(arguments.get("date"))
        slots: list[dict[str, Any]] = []
        searches: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for wanted in arguments.get("slots") or []:
            course = str(wanted.get("course"))
            at = str(wanted.get("time"))[:5].zfill(5)
            search, found = self._found.get((chat_id, course, day), (None, []))
            slot = next((s for s in found if s["start"][11:16] == at), None)
            if slot is None:
                missing.append(f"{course} {at}")
            elif slot not in slots:
                slots.append(slot)
                searches[course] = search
        if not slots:
            content = (
                f"None of {missing} is in a search result for {day}; search first "
                "(search_tee_times) and offer only slots it found."
            )
            return {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": content,
                "is_error": True,
            }
        offer = TeeTimeOffer(
            id=secrets.token_urlsafe(8),
            chat_id=chat_id,
            searches=searches,
            slots=tuple(slots[:MAX_OFFERED]),
        )
        for stale in [o.id for o in self._offers.values() if o.expired]:
            del self._offers[stale]
        self._offers[offer.id] = offer
        reply.offers.append(offer)
        shown = [f"{s['course']} {s['start']}" for s in offer.slots]
        content = f"Shown as a card with Book buttons: {shown}."
        if missing:
            content += f" Not in the results, so left out: {missing}."
        return {"type": "tool_result", "tool_use_id": call.id, "content": content}

    def offer(self, offer_id: str) -> TeeTimeOffer | None:
        """A card's slots, for as long as its buttons work. Not used up by a
        tap: a Book only asks to confirm, so the person may pick another."""
        offer = self._offers.get(offer_id)
        if offer is None or offer.expired:
            return None
        return offer

    def hold_slot(self, offer: TeeTimeOffer, slot: dict[str, Any]) -> PendingPayment:
        """A Book tap: booking that one slot, waiting on Confirm."""
        payment = PendingPayment(
            id=secrets.token_urlsafe(8),
            chat_id=offer.chat_id,
            tool=BOOK_SLOT,
            arguments={"search": offer.searches[slot["course"]], "slot": slot},
        )
        for stale in [p.id for p in self._payments.values() if p.expired]:
            del self._payments[stale]
        self._payments[payment.id] = payment
        return payment

    async def _book_slot(self, arguments: dict[str, Any]) -> ToolResult:
        """A one-off request for exactly this slot, then a run on it."""
        search = arguments["search"]
        start = datetime.fromisoformat(arguments["slot"]["start"])
        request = {
            "user_email": search["user_email"] or self._settings.user_email,
            "course": search["course"],
            "target_date": start.date().isoformat(),
            "earliest": start.time().isoformat(),
            "latest": start.time().isoformat(),
            "holes": search["holes"],
            "players": search["players"],
        }
        if search.get("env_prefix"):
            request["env_prefix"] = search["env_prefix"]
        created = await self._tools.call("create_request", request)
        if created.is_error:
            return created
        return await self._tools.call(
            "run_request",
            {
                "request_id": created.json()["id"],
                "submit_payment": self._pays,
                "wait_for_release": False,
            },
        )

    def take_payment(self, payment_id: str) -> PendingPayment | None:
        """The pending payment for a button, once; None if unknown or stale."""
        payment = self._payments.pop(payment_id, None)
        if payment is None or payment.expired:
            return None
        return payment

    async def confirm(
        self, payment: PendingPayment
    ) -> tuple[ToolResult, StartedRun | None]:
        """Run a payment the person confirmed, and tell the conversation.

        The run is None when the call failed, or when it scheduled a booking
        for later rather than starting one."""
        if payment.tool == BOOK_SLOT:
            result = await self._book_slot(payment.arguments)
            what = f"booking {payment.arguments['slot']['start']} from the card"
        else:
            result = await self._tools.call(payment.tool, payment.arguments)
            what = payment.tool
        starts_run = payment.tool == BOOK_SLOT or payment.tool in RUN_TOOLS
        run = None
        if result.is_error:
            outcome = f"it failed: {result.text[:300]}"
        elif starts_run:
            run = StartedRun.from_result(result)
            outcome = f"run {run.run_id} started"
        else:
            outcome = "it's scheduled"
        await self.note(
            payment.chat_id, f"[The person tapped Confirm on {what}; {outcome}.]"
        )
        return result, run

    async def explain_failure(
        self, chat_id: int, run: StartedRun, outcome: dict[str, Any], course: str
    ) -> Reply:
        """A run that ended in an error, put to the model to tell the person
        about (and, where it can, help with) rather than sent as is."""
        kind = "scheduled booking" if outcome.get("scheduled") else "booking"
        mode = " (a dry run)" if run.dry_run else ""
        request_id = outcome.get("request_id")
        lines = [
            f"The {kind}{mode} at {course} ({run.course}) on {run.target_date} "
            f"failed: {outcome.get('detail') or 'no detail'}.",
            f"Run {run.run_id}, request {request_id}.",
        ]
        # What was asked for, so the follow-up can offer something close to it.
        looked_up = await self._tools.call("get_request", {"request_id": request_id})
        if not looked_up.is_error:
            with suppress(KeyError, TypeError, ValueError):
                lines.extend(_asked_for(looked_up.json()))
        lines.append("The person hasn't been told yet.")
        return await self.respond(chat_id, f"[{' '.join(lines)}]")

    async def note(self, chat_id: int, text: str) -> None:
        """Something the conversation should know that happened outside it."""
        async with self._lock(chat_id):
            history = self._history.get(chat_id)
            if history:
                history.append({"role": "user", "content": text})

    def reset(self, chat_id: int) -> None:
        self._history.pop(chat_id, None)
        for payment_id in [
            p.id for p in self._payments.values() if p.chat_id == chat_id
        ]:
            del self._payments[payment_id]
        for offer_id in [o.id for o in self._offers.values() if o.chat_id == chat_id]:
            del self._offers[offer_id]
        for key in [k for k in self._found if k[0] == chat_id]:
            del self._found[key]


def _asked_for(request: dict[str, Any]) -> list[str]:
    """A failed run's request, for the note that puts the failure to the model."""
    lines = [
        f"It asked for {request['earliest'][:5]}-{request['latest'][:5]}, "
        f"{request['players']} players, {request['holes']} holes."
    ]
    if request.get("plan_id"):
        lines.append(
            f"It was this date of weekly plan {request['plan_id']}, which stays "
            "scheduled for the weeks after."
        )
    return lines


def _text(content: list[Any]) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()


def _stop_text(response: Message) -> str:
    if response.stop_reason == "refusal":
        return "The model declined to answer that one."
    if response.stop_reason == "max_tokens":
        return "That answer ran too long; try a narrower question."
    return "(no reply)"
