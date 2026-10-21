"""The conversation: a message in, Claude and the booking tools, a reply out.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import anthropic
from anthropic.types import MessageParam, ToolResultBlockParam, ToolUseBlock
from loguru import logger

from tee_time_booker.bot.tools import (
    PAYING_TOOLS,
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

How booking works here:
- A plan is a standing wish: a course, a weekday some weeks ahead, a tee-off window, a \
group size and holes. list_plans shows each plan with the date it would book next.
- find_tee_times lists what's open on a plan's date (or another date) without booking. \
It drives a real browser, so it takes a minute or two; say so before calling it.
- book_plan and run_request start a run in the background and answer at once with a \
run id. The bot watches every run and messages the person when it ends, so don't poll \
get_run unless they ask how a run is going.
- A run takes the best slot inside the window; it can't be aimed at one particular \
slot. To get a particular time, make a one-off request (create_request) with a narrow \
window around it, then run_request. One-off requests need user_email {email} and the \
env_prefix of the person's account on that course's site (list_plans shows it).
- submit_payment=false is a dry run: it stops at checkout and pays nothing. Set \
submit_payment=true only when the person has clearly asked to book for real. That \
call is held: the person gets a Confirm / Cancel button and nothing is paid until they \
tap Confirm. Don't repeat the call; point them at the button.
- {payment_switch}
- Before booking, check what's open with find_tee_times unless the person says not \
to, and say back the course, date, window and group size you're about to book.
- People speak in their own terms ("this Saturday", "after 9"). Work out the actual \
date from today's date below and say it back.

Replies are read on a phone: plain text, no Markdown, short. Write times like 9:12 AM.
Today is {today:%A %B %-d, %Y}."""

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


@dataclass
class Reply:
    text: str
    payments: list[PendingPayment] = field(default_factory=list)
    runs: list[StartedRun] = field(default_factory=list)


class BookingAgent:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
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

    def _lock(self, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault(chat_id, asyncio.Lock())

    def _system(self) -> str:
        return SYSTEM.format(
            email=self._settings.user_email,
            payment_switch=PAYMENT_ON if self._pays else PAYMENT_OFF,
            today=self._today(),
        )

    async def respond(self, chat_id: int, text: str) -> Reply:
        """Answer one message from the person, running tools as the model asks."""
        async with self._lock(chat_id):
            if self._tool_defs is None:
                self._tool_defs = await self._tools.definitions()
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
                        reply.text = _text(response.content) or _stop_text(response)
                        break
                    calls = [b for b in response.content if b.type == "tool_use"]
                    results = await asyncio.gather(
                        *(self._run_tool(chat_id, call, reply) for call in calls)
                    )
                    turn.append({"role": "user", "content": list(results)})
                else:
                    reply.text = "That took too many steps; try asking more simply."
            except anthropic.APIError as e:
                logger.exception("Claude request failed")
                reply.text = f"Couldn't reach Claude ({type(e).__name__}). Try again."
                return reply
            history.extend(turn)
            return reply

    async def _run_tool(
        self, chat_id: int, call: ToolUseBlock, reply: Reply
    ) -> ToolResultBlockParam:
        arguments = dict(call.input)
        if self._pays and asks_to_pay(call.name, arguments):
            payment = PendingPayment(
                id=secrets.token_urlsafe(8),
                chat_id=chat_id,
                tool=call.name,
                arguments=arguments,
            )
            self._payments[payment.id] = payment
            reply.payments.append(payment)
            content = (
                "Held for the person's confirmation: they now have a Confirm / Cancel "
                "button. Nothing has been booked or paid yet."
            )
            return {"type": "tool_result", "tool_use_id": call.id, "content": content}
        logger.info(f"Tool {call.name} {arguments}")
        result = await self._tools.call(call.name, arguments)
        if call.name in PAYING_TOOLS and not result.is_error:
            reply.runs.append(StartedRun.from_result(result))
        return {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": result.text,
            "is_error": result.is_error,
        }

    def take_payment(self, payment_id: str) -> PendingPayment | None:
        """The pending payment for a button, once; None if unknown or stale."""
        payment = self._payments.pop(payment_id, None)
        if payment is None or payment.expired:
            return None
        return payment

    async def confirm(
        self, payment: PendingPayment
    ) -> tuple[ToolResult, StartedRun | None]:
        """Run a payment the person confirmed, and tell the conversation."""
        result = await self._tools.call(payment.tool, payment.arguments)
        run = None if result.is_error else StartedRun.from_result(result)
        outcome = (
            f"run {run.run_id} started" if run else f"it failed: {result.text[:300]}"
        )
        await self.note(payment.chat_id, f"[The person tapped Confirm; {outcome}.]")
        return result, run

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


def _text(content: list[Any]) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()


def _stop_text(response: anthropic.types.Message) -> str:
    if response.stop_reason == "refusal":
        return "Claude declined to answer that one."
    if response.stop_reason == "max_tokens":
        return "That answer ran too long; try a narrower question."
    return "(no reply)"
