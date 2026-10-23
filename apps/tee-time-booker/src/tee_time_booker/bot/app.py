"""The Telegram side: who may talk to the bot, buttons, voice, run updates.

    python -m tee_time_booker.bot

Long polling, so the machine opens no ports: the bot asks Telegram for new
messages. Every update from anyone outside `TELEGRAM_ALLOWED_USER_IDS` is
dropped before any handler sees it.

The API, its booking worker and the MCP tools all live in this process (see
`agent.py` for the conversation). A run started from a chat is watched here
and its outcome sent to that chat the moment the worker finishes it; a run
that failed is handed to the model instead, to explain and offer what's open.
The watch is in memory: a run still going when the bot stops isn't reported
after a restart (`/runs` shows it).

The scheduler runs here too: every minute it starts whatever auto-booked
plans and requests are due (`tee_time_booker.scheduler`), and each such run's
outcome goes to every allowed user's private chat. A day that turns out not
to be open yet isn't reported; the scheduler simply tries again later.
"""

import asyncio
import html
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from contextlib import AsyncExitStack, suppress
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from fastmcp import Client
from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from tee_time_booker.api import create_app
from tee_time_booker.bot.agent import (
    BOOK_SLOT,
    BookingAgent,
    PendingPayment,
    Reply,
    StartedRun,
    TeeTimeOffer,
)
from tee_time_booker.bot.llm import make_model
from tee_time_booker.bot.rich import (
    Progress,
    edit_rich,
    escape,
    escape_prices,
    send_rich,
)
from tee_time_booker.bot.tools import BookingTools
from tee_time_booker.bot.voice import Transcriber, VoiceUnavailable
from tee_time_booker.config import BotSettings, Settings
from tee_time_booker.mcp import create_server
from tee_time_booker.scheduler import Started, start_due

# How often a watched run is checked when this process isn't the one running
# it (so there's no future to wait on). Runs take minutes, or up to 95 min
# when waiting on a release.
RUN_POLL_S = 30
# How often the scheduler looks for auto-booked runs that are due.
SCHEDULE_TICK_S = 60
# Consecutive failed checks before a watch gives up (10 minutes' worth).
MAX_WATCH_ERRORS = 20
# Telegram's limits: 4096 characters per plain message, 32768 per rich one.
MAX_MESSAGE = 4000
MAX_RICH_MESSAGE = 30000
# How long a message to the chat keeps retrying through a dropped connection
# before it's given up on, and the longest wait between tries.
SEND_RETRY_S = 15 * 60
SEND_BACKOFF_MAX_S = 30

# Rich Markdown: a single newline doesn't break a line, a blank one does.
HELP = """\
**Tell me what you'd like to book**, by text or voice note. For example:

- what's open at Sterling Hills this Saturday morning?
- book my Soule plan for next Saturday, 4 players after 9
- book Glen Annie on Nov 14, 8–10 AM, as soon as it opens
- every Sunday, get me a 9 AM tee time at Olivas Links three weeks out
- how did my last booking go?

Real payments always wait for you to tap **Confirm**; for a booking you \
schedule, that tap is when you schedule it. {payment_mode}

- /new starts a fresh conversation
- /runs shows recent runs"""

RUN_STATUS = {
    "running": "⏳ running",
    "success": "✅ booked",
    "dry_run": "🧪 dry run",
    "unknown": "⚠️ unconfirmed",
    "error": "❌ failed",
    "not_open": "📭 not open yet",
}
WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


class BookingBot:
    def __init__(
        self,
        agent: BookingAgent,
        tools: BookingTools,
        transcriber: Transcriber,
        settings: BotSettings,
        *,
        pays: bool,
        running: Callable[[str], Future | None] = lambda run_id: None,
    ) -> None:
        self.agent = agent
        self.tools = tools
        self.transcriber = transcriber
        self.settings = settings
        self.pays = pays
        # The worker's future for a run still going in this process, if any.
        self.running = running
        # Plain asyncio tasks, not `Application.create_task`: the Application
        # awaits its own tasks on stop, and a watch can run for 95 minutes.
        self._watches: set[asyncio.Task] = set()
        self._scheduler: asyncio.Task | None = None

    def install(self, application: Application) -> None:
        # Group -1 runs before every other handler; raising stops the update.
        application.add_handler(TypeHandler(Update, self._gate), group=-1)
        application.add_handler(CommandHandler(["start", "help"], self._help))
        application.add_handler(CommandHandler("new", self._new))
        application.add_handler(CommandHandler("runs", self._runs))
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text)
        )
        application.add_handler(
            MessageHandler(filters.VOICE | filters.AUDIO, self._on_voice)
        )
        application.add_handler(CallbackQueryHandler(self._on_button))
        application.add_error_handler(self._on_error)

    # --- handlers ---------------------------------------------------------

    async def _gate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.id not in self.settings.allowed_user_ids:
            logger.warning(
                f"Ignored an update from Telegram user {user.id if user else '?'} "
                f"({user.username if user else 'no user'})"
            )
            raise ApplicationHandlerStop

    async def _help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        mode = (
            "**Payment is ON:** a confirmed booking really pays."
            if self.pays
            else "**Payment is OFF** (`TEE_TIME_SUBMIT_PAYMENT`): every run is a "
            "dry run."
        )
        await self._send(
            context, update.effective_chat.id, HELP.format(payment_mode=mode)
        )

    async def _new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.agent.reset(update.effective_chat.id)
        await update.effective_message.reply_text("Starting fresh.")

    async def _runs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        result = await self.tools.call("list_runs", {"limit": 5})
        if result.is_error:
            await self._send(context, chat_id, escape(result.text))
            return
        await self._send(context, chat_id, format_runs(result.json()))

    async def _on_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._converse(update, context, update.effective_message.text)

    async def _on_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        audio = message.voice or message.audio
        await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.ogg"
            await (await audio.get_file()).download_to_drive(path)
            try:
                text = await asyncio.to_thread(self.transcriber.transcribe, path)
            except VoiceUnavailable as e:
                await message.reply_text(str(e))
                return
        if not text:
            await message.reply_text("I couldn't make out any words in that.")
            return
        # Said back first, so a mishearing is visible before anything is done.
        await self._send(context, message.chat_id, f"> 🎙 {escape(text)}")
        await self._converse(update, context, text)

    async def _on_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        action, _, payment_id = (query.data or "").partition(":")
        if action == "book":
            await self._on_book(context, query, payment_id)
            return
        payment = self.agent.take_payment(payment_id)
        if payment is None or payment.chat_id != query.message.chat_id:
            await query.answer("That button has expired. Ask me again.")
            await query.edit_message_reply_markup(None)
            return
        await query.answer()
        if action != "pay":
            await self._close_prompt(
                context, query, payment, "Cancelled. Nothing was paid."
            )
            await self.agent.note(payment.chat_id, "[The person tapped Cancel.]")
            return
        # A run starts in the background, so this answers at once; the card
        # then says how it went rather than a message of its own.
        result, run = await self.agent.confirm(payment)
        if result.is_error:
            await self._close_prompt(
                context, query, payment, f"That didn't work: {result.text[:300]}"
            )
            return
        if run is None:
            await self._close_prompt(
                context, query, payment, scheduled_status(result.json())
            )
            return
        kind = "Dry run" if run.dry_run else "Booking"
        await self._close_prompt(
            context,
            query,
            payment,
            f"{kind} started. I'll message you when it finishes.",
        )
        self._watch(context.bot, payment.chat_id, run)

    async def _on_book(
        self, context: ContextTypes.DEFAULT_TYPE, query: Any, data: str
    ) -> None:
        """A Book tap on a tee-time card: ask to confirm that one slot. The
        card keeps its buttons, so another slot can still be picked."""
        offer_id, _, index = data.partition(":")
        offer = self.agent.offer(offer_id)
        slot = None
        if offer is not None and offer.chat_id == query.message.chat_id:
            with suppress(ValueError, IndexError):
                slot = offer.slots[int(index)]
        if slot is None:
            await query.answer("Those times have expired. Ask me again.")
            await query.edit_message_reply_markup(None)
            return
        await query.answer()
        await self._ask_to_pay(context.bot, self.agent.hold_slot(offer, slot))

    async def _close_prompt(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        query: Any,
        payment: PendingPayment,
        status: str,
    ) -> None:
        """Swap the Confirm / Cancel buttons for what became of them
        (`status`, plain text).

        The prompt is drawn again rather than read back: a rich message has
        no `text`, and python-telegram-bot doesn't parse its content yet.
        """
        card = await self._describe_payment(payment)
        try:
            await edit_rich(
                context.bot,
                payment.chat_id,
                query.message.message_id,
                f"{card}\n\n<footer>{html.escape(status)}</footer>",
            )
        except BadRequest:
            logger.exception("Editing the payment prompt")
            await query.edit_message_reply_markup(None)
            await self._send(context, payment.chat_id, escape(status))

    async def _on_error(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        error = context.error
        if isinstance(error, NetworkError) and not isinstance(error, BadRequest):
            # A connection blip. Polling retries by itself, and there's no
            # point telling a chat we can't reach.
            logger.warning(f"Telegram unreachable: {error}")
            return
        logger.opt(exception=error).error("Telegram handler failed")
        if isinstance(update, Update) and update.effective_chat:
            await self._send(
                context,
                update.effective_chat.id,
                f"Something went wrong: {escape(error)}",
            )

    # --- helpers ----------------------------------------------------------

    async def _converse(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
    ) -> None:
        chat_id = update.effective_chat.id
        # The incoming message's id is unique per turn, so it names the draft.
        async with Progress(
            context.bot, chat_id, update.effective_message.message_id
        ) as progress:
            reply = await self.agent.respond(chat_id, text, on_step=progress.step)
        await self._deliver(context.bot, chat_id, reply)

    async def _deliver(self, bot: Any, chat_id: int, reply: Reply) -> None:
        """Everything a reply holds: its words, cards, Confirm prompts, and
        runs to watch."""
        if reply.text:
            await self._say(bot, chat_id, escape_prices(reply.text))
        for offer in reply.offers:
            await self._show_offer(bot, offer)
        for payment in reply.payments:
            await self._ask_to_pay(bot, payment)
        for run in reply.runs:
            self._watch(bot, chat_id, run)

    async def _show_offer(self, bot: Any, offer: TeeTimeOffer) -> None:
        # A keyboard under the card, not buttons in its table: Telegram shows
        # those but (as of Bot API 10.3) doesn't send the bot their taps.
        numbered = len(offer.searches) > 1
        buttons = [
            InlineKeyboardButton(
                slot_label(slot, i + 1 if numbered else None),
                callback_data=f"book:{offer.id}:{i}",
            )
            for i, slot in enumerate(offer.slots)
        ]
        keyboard = InlineKeyboardMarkup(
            [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
        )
        names = {}
        for slot in offer.slots:
            key = slot["course"]
            if key not in names:
                names[key] = slot.get("course_name") or await self._course_name(key)
        await send_with_retry(
            bot,
            offer.chat_id,
            format_offer(offer, names),
            reply_markup=keyboard,
        )

    async def _course_name(self, key: str) -> str:
        looked_up = await self.tools.call("get_course", {"key": key})
        return key if looked_up.is_error else looked_up.json()["name"]

    async def _ask_to_pay(self, bot: Any, payment: PendingPayment) -> None:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Confirm and pay" if self.pays else "Start dry run",
                        callback_data=f"pay:{payment.id}",
                        style="success" if self.pays else "primary",
                    ),
                    InlineKeyboardButton(
                        "Cancel", callback_data=f"cancel:{payment.id}"
                    ),
                ]
            ]
        )
        text = await self._describe_payment(payment)
        await send_with_retry(bot, payment.chat_id, text, reply_markup=keyboard)

    async def _describe_payment(self, payment: PendingPayment) -> str:
        """What a Confirm will book, read from the plan or request itself
        rather than from anything the model said. Only a booking that doesn't
        exist yet (a slot from a card, a new schedule) is described from the
        call, and the call is exactly what a Confirm runs."""
        fallback = (
            f"**💳 Book and pay?**\n\n{escape(f'{payment.tool} {payment.arguments}')}"
        )
        try:
            if payment.tool == BOOK_SLOT:
                search = payment.arguments["search"]
                course = await self._course_name(str(search["course"]))
                return self._slot_lines(payment.arguments["slot"], search, course)
            if payment.tool in ("create_plan", "create_request"):
                course = await self._course_name(str(payment.arguments["course"]))
                return schedule_lines(payment.tool, payment.arguments, course)
            if payment.tool in ("book_plan", "set_plan_auto_book"):
                looked_up = await self.tools.call(
                    "get_plan", {"plan_id": payment.arguments.get("plan_id")}
                )
            else:
                looked_up = await self.tools.call(
                    "get_request", {"request_id": payment.arguments.get("request_id")}
                )
            if looked_up.is_error:
                return fallback
            row = looked_up.json()
            course = await self._course_name(row["course"])
            if payment.tool == "set_plan_auto_book":
                return schedule_lines("create_plan", row, course)
            return self._payment_lines(payment, row, course)
        except (KeyError, TypeError, ValueError):
            # Malformed model arguments (e.g. an unparseable target_date) must
            # not cost the person their Confirm / Cancel buttons.
            logger.exception("Describing a held payment")
            return fallback

    def _slot_lines(
        self, slot: dict[str, Any], row: dict[str, Any], course: str
    ) -> str:
        start = datetime.fromisoformat(slot["start"])
        side = f" ({slot['side']})" if slot.get("side") else ""
        details = {
            "Course": course,
            "Date": format_date(start.date().isoformat()),
            "Tee-off": f"{format_time(start.time().isoformat())}{side}",
            "Group": f"{row['players']} players · {row['holes']} holes",
        }
        if slot.get("green_fee") is not None:
            per_player = slot["green_fee"] + (slot.get("service_fee") or 0)
            details["Green fee"] = f"{format_money(slot['green_fee'])} / player"
            details["Total"] = f"about {format_money(per_player * row['players'])}"
        lines = [
            "**💳 Book this tee time?**"
            if self.pays
            else "**🧪 Dry run this tee time?**",
            detail_table(details),
            "_Pays at checkout when you confirm._"
            if self.pays
            else "_Payment is off: the run stops at checkout and pays nothing._",
        ]
        return "\n\n".join(lines)

    @staticmethod
    def _payment_lines(
        payment: PendingPayment, row: dict[str, Any], course: str
    ) -> str:
        target = (
            payment.arguments.get("target_date")
            or row.get("target_date")
            or row.get("next_target_date")
        )
        which = "earliest" if row["prefer_earlier"] else "latest"
        details = {
            "Course": course,
            "Date": format_date(target),
            "Tee-off": f"{format_time(row['earliest'])} – {format_time(row['latest'])}",
            "Group": f"{row['players']} players · {row['holes']} holes",
        }
        lines = [
            "**💳 Book and pay?**",
            detail_table(details),
            f"_The run takes the {which} open slot in that window and pays at "
            "checkout._",
        ]
        if payment.arguments.get(
            "wait_for_release", True
        ) and not payment.arguments.get("target_date"):
            lines.append(
                "_If that day isn't released yet, it waits for the release first._"
            )
        return "\n\n".join(lines)

    def _watch(
        self, bot: Any, chat_id: int, run: StartedRun, *, scheduled: bool = False
    ) -> None:
        task = asyncio.create_task(self._follow(bot, chat_id, run, scheduled))
        self._watches.add(task)
        task.add_done_callback(self._watch_done)

    def _watch_done(self, task: asyncio.Task) -> None:
        self._watches.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.opt(exception=task.exception()).error("Watching a run failed")

    async def stop_watching(self) -> None:
        """Stop the scheduler and drop every watch (the runs themselves carry
        on; `/runs` shows them)."""
        tasks = [*self._watches, *([self._scheduler] if self._scheduler else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _follow(
        self, bot: Any, chat_id: int, run: StartedRun, scheduled: bool = False
    ) -> None:
        errors = 0
        course = await self._course_name(run.course)
        while True:
            await self._until_done(run.run_id)
            result = await self.tools.call("get_run", {"run_id": run.run_id})
            if result.is_error:
                logger.warning(f"Checking run {run.run_id}: {result.text}")
                errors += 1
                if errors >= MAX_WATCH_ERRORS:
                    await self._say(
                        bot,
                        chat_id,
                        f"I lost track of the run for {escape(course)}; /runs "
                        "shows it.",
                    )
                    return
                continue
            errors = 0
            body = result.json()
            if body["status"] == "running":
                continue
            if scheduled and body["status"] == "not_open":
                return  # the scheduler tries again later
            if body["status"] == "error" and await self._explain(
                bot, chat_id, run, body, course
            ):
                return
            text = format_outcome(run, body, course)
            if scheduled:
                text = f"⏰ **Scheduled booking**\n\n{text}"
            await self._say(bot, chat_id, text)
            return

    async def _until_done(self, run_id: str) -> None:
        """Wait for the worker to finish `run_id`, or one poll interval when
        it isn't running here (or is already done and forgotten)."""
        future = self.running(run_id)
        if future is None:
            await asyncio.sleep(RUN_POLL_S)
            return
        # Shielded: dropping a watch must not cancel a run still queued.
        # `execute` records every outcome itself, so its result isn't needed.
        with suppress(Exception):
            await asyncio.shield(asyncio.wrap_future(future))

    async def _explain(
        self, bot: Any, chat_id: int, run: StartedRun, body: dict[str, Any], course: str
    ) -> bool:
        """Have the model tell the person about a failed run, and offer what
        it can instead. False when it couldn't, so the plain outcome goes out
        after all."""
        try:
            reply = await self.agent.explain_failure(chat_id, run, body, course)
        except Exception:
            logger.exception(f"Explaining failed run {run.run_id}")
            return False
        if reply.failed or not (reply.text or reply.offers):
            return False
        await self._deliver(bot, chat_id, reply)
        return True

    # --- scheduler ----------------------------------------------------------

    def start_scheduler(self, bot: Any, tick: Callable[[], list[Started]]) -> None:
        """Run `tick` (`scheduler.start_due`) every minute until stopped."""
        self._scheduler = asyncio.create_task(self._schedule_forever(bot, tick))

    async def _schedule_forever(
        self, bot: Any, tick: Callable[[], list[Started]]
    ) -> None:
        while True:
            await self.schedule_once(bot, tick)
            await asyncio.sleep(SCHEDULE_TICK_S)

    async def schedule_once(self, bot: Any, tick: Callable[[], list[Started]]) -> None:
        """One tick: start what's due, and watch each run for every allowed
        user (their private chat's id is their user id)."""
        try:
            # On a thread: it reads the database and may start runs.
            started = await asyncio.to_thread(tick)
        except Exception:
            logger.exception("Scheduler tick failed")
            return
        for item in started:
            claimed = item.claimed
            run = StartedRun(
                run_id=str(claimed.run_id),
                course=claimed.course.key,
                target_date=claimed.criteria.target_date.isoformat(),
                dry_run=item.dry_run,
            )
            for chat_id in sorted(self.settings.allowed_user_ids):
                self._watch(bot, chat_id, run, scheduled=True)

    async def _send(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str
    ) -> None:
        await self._say(context.bot, chat_id, text)

    @staticmethod
    async def _say(bot: Any, chat_id: int, text: str) -> None:
        for chunk in _chunks(text, MAX_RICH_MESSAGE):
            await send_with_retry(bot, chat_id, chunk)


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] or "…" for i in range(0, max(len(text), 1), size)]


async def send_with_retry(bot: Any, chat_id: int, text: str, **kwargs: Any) -> None:
    """Send a rich message, riding out a dropped connection or a rate limit.

    A run's outcome may be the only word the person gets about it, so a blip
    shouldn't lose it. Gives up (raising) after `SEND_RETRY_S`. A BadRequest
    is a fault in the message itself and isn't retried; for a rich message
    it's most likely the formatting, so the words go again as plain text.
    """
    try:
        await _retrying(chat_id, lambda: send_rich(bot, chat_id, text, **kwargs))
        return
    except BadRequest as e:
        logger.warning(f"Rich message to chat {chat_id} refused ({e}); sending plain")
    chunks = _chunks(text, MAX_MESSAGE)
    for i, chunk in enumerate(chunks):
        # Any buttons go on the last part only.
        extra = kwargs if i == len(chunks) - 1 else {}
        await _retrying(chat_id, lambda: bot.send_message(chat_id, chunk, **extra))


async def _retrying(chat_id: int, send: Callable[[], Awaitable[Any]]) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SEND_RETRY_S
    backoff = 1.0
    while True:
        try:
            await send()
            return
        except RetryAfter as e:
            wait = e.retry_after
            wait = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
        except BadRequest:
            raise
        except NetworkError as e:
            if loop.time() >= deadline:
                raise
            logger.warning(f"Sending to chat {chat_id} failed ({e}); retrying")
            wait, backoff = backoff, min(backoff * 2, SEND_BACKOFF_MAX_S)
        await asyncio.sleep(wait)


# --- formatting -------------------------------------------------------------


def format_date(value: str | None) -> str:
    if not value:
        return "?"
    d = date.fromisoformat(value)
    return f"{d:%a %b} {d.day}"


def format_time(value: str) -> str:
    t = time.fromisoformat(value)
    return f"{t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'}"


def format_money(value: float) -> str:
    return f"${value:,.2f}".removesuffix(".00")


def format_moment(value: datetime) -> str:
    """A moment in its own (course-local) time, e.g. 'Sat Oct 31, 11:50 PM'."""
    return f"{value:%a %b} {value.day}, {format_time(value.time().isoformat())}"


def _ordinal(n: int) -> str:
    suffix = (
        "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    )
    return f"{n}{suffix}"


def schedule_lines(tool: str, booking: dict[str, Any], course: str) -> str:
    """The Confirm card for scheduling a booking: `booking` is a create_plan
    or create_request call's arguments, or a plan the card would arm."""
    details = {"Course": course}
    if tool == "create_plan":
        weekday = WEEKDAYS[int(booking["target_weekday"])]
        details["Day"] = (
            f"{weekday}s, the {_ordinal(int(booking['weeks_ahead']))} ahead"
        )
        if booking.get("next_target_date"):
            details["Next date"] = format_date(booking["next_target_date"])
        title = "**⏰ Book this every week, automatically?**"
        footer = (
            "_Books each next date as soon as it opens and pays at checkout, "
            "without asking again. Pause it any time._"
        )
    else:
        details["Date"] = format_date(str(booking["target_date"]))
        title = "**⏰ Book this automatically?**"
        footer = (
            "_Books it as soon as the day opens and pays at checkout, without "
            "asking again. Cancel it any time before then._"
        )
    details["Tee-off"] = (
        f"{format_time(str(booking['earliest']))} – "
        f"{format_time(str(booking['latest']))}"
    )
    details["Group"] = f"{booking['players']} players · {booking['holes']} holes"
    return "\n\n".join([title, detail_table(details), footer])


def scheduled_status(booking: dict[str, Any]) -> str:
    """What a confirmed schedule card says: when it books, from the plan or
    request the API returned."""
    target = booking.get("next_target_date") or booking.get("target_date")
    when = ""
    if booking.get("first_attempt_at"):
        first = datetime.fromisoformat(booking["first_attempt_at"])
        if first > datetime.now(first.tzinfo):
            when = f" First try {format_moment(first)}."
        else:
            when = " It's bookable already, so it tries at the next chance."
    return f"Scheduled for {format_date(target)}.{when} I'll message you how it goes."


def detail_table(details: dict[str, str]) -> str:
    """Label / value rows. HTML, since a Markdown table needs a header row."""
    cells = "".join(
        f"<tr><td><b>{label}</b></td><td>{html.escape(value)}</td></tr>"
        for label, value in details.items()
    )
    return f"<table compact>{cells}</table>"


def slot_label(slot: dict[str, Any], number: int | None = None) -> str:
    """A tee-time slot on a button: its time (the card's table has the price),
    and its row number when the card spans courses."""
    label = format_time(datetime.fromisoformat(slot["start"]).time().isoformat())
    return f"#{number} {label}" if number is not None else label


def format_offer(offer: TeeTimeOffer, names: dict[str, str]) -> str:
    """The card for `offer`; `names` maps its course keys to course names."""
    day = format_date(offer.slots[0]["start"][:10])
    several = len(names) > 1
    title = day if several else f"{escape(next(iter(names.values())))} · {day}"
    count = len(offer.slots)
    about = [f"{count} {'time' if count == 1 else 'times'}"]
    search = next(iter(offer.searches.values()))
    about += [f"{search['players']} golfers", f"{search['holes']} holes"]
    if several:
        about.append(f"{len(names)} courses")
    # A Side column only when it tells the slots apart.
    sides = len({slot.get("side") for slot in offer.slots}) > 1
    head = '<th align="left">#</th><th align="left">Course</th>' if several else ""
    head += '<th align="left">Time</th>'
    if sides:
        head += '<th align="left">Side</th>'
    head += '<th align="right">Per player</th>'
    rows = []
    for number, slot in enumerate(offer.slots, 1):
        start = datetime.fromisoformat(slot["start"])
        row = ""
        if several:
            row += f"<td>{number}</td><td>{html.escape(names[slot['course']])}</td>"
        row += f"<td><b>{format_time(start.time().isoformat())}</b></td>"
        if sides:
            row += f"<td>{html.escape(slot.get('side') or '')}</td>"
        fee = slot.get("green_fee")
        price = f"<b>{format_money(fee)}</b>" if fee is not None else "–"
        row += f'<td align="right">{price}</td>'
        rows.append(f"<tr>{row}</tr>")
    return "\n\n".join(
        [
            f"📅 **{title}**",
            " · ".join(about),
            f"<table striped compact><tr>{head}</tr>{''.join(rows)}</table>",
            "_Tap a time to book it._",
        ]
    )


def _slot(run: dict[str, Any]) -> str:
    if not run.get("tee_time_local"):
        return ""
    start = datetime.fromisoformat(run["tee_time_local"])
    side = f" ({run['side']})" if run.get("side") else ""
    return f"{format_time(start.time().isoformat())}{side}"


def format_outcome(
    started: StartedRun, run: dict[str, Any], course: str | None = None
) -> str:
    """`course` is the course's name; without it the key stands in."""
    name = escape(course or started.course)
    where = f"{name} on {format_date(started.target_date)}"
    slot = escape(_slot(run))
    fee = (
        f"\n\nGreen fee {escape(format_money(float(run['green_fee'])))} per player."
        if run.get("green_fee")
        else ""
    )
    match run["status"]:
        case "success":
            # In code, so it's one tap to copy.
            confirmation = (
                f"\n\nConfirmation: `{run['confirmation'].replace('`', '')}`"
                if run.get("confirmation")
                else ""
            )
            at = f" at **{slot}**" if slot else ""
            return f"✅ **Booked** {where}{at}.{fee}{confirmation}"
        case "dry_run":
            return (
                f"🧪 **Dry run done:** reached checkout for {slot or 'a slot'} at "
                f"{where}. Nothing was paid.{fee}"
            )
        case "unknown":
            return (
                f"⚠️ **Payment submitted, no confirmation** for {where} {slot}.\n\n"
                "Check your account on the booking site before trying again."
            )
        case "not_open":
            return (
                f"📭 **Not open yet:** {where} isn't in the site's booking window "
                "yet. Nothing was booked."
            )
        case _:
            detail = html.escape(run.get("detail") or "no detail")
            return (
                f"❌ **The run failed** for {where}.\n\n"
                f"<blockquote expandable>{detail}</blockquote>"
            )


def format_runs(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "No runs yet."
    rows = ["| Started | Status | Slot |", "|:--|:--|:--|"]
    for run in runs:
        started = datetime.fromisoformat(run["started_at"]).astimezone()
        status = RUN_STATUS.get(run["status"], escape(run["status"]))
        rows.append(
            f"| {started:%a %b} {started.day} {format_time(started.time().isoformat())}"
            f" | {status} | {escape(_slot(run)) or '–'} |"
        )
    return "\n".join(rows)


# --- entry point --------------------------------------------------------------


def build_application() -> Application:
    settings = Settings.from_env()
    bot_settings = BotSettings.from_env()
    api = create_app(settings)
    client = Client(create_server(api))
    tools = BookingTools(client)
    agent = BookingAgent(
        make_model(bot_settings),
        tools,
        bot_settings,
        pays=settings.submit_payment,
    )
    bot = BookingBot(
        agent,
        tools,
        Transcriber(bot_settings.whisper_model),
        bot_settings,
        pays=settings.submit_payment,
        running=lambda run_id: api.state.worker.futures.get(uuid.UUID(run_id)),
    )
    stack = AsyncExitStack()

    async def started(application: Application) -> None:
        await stack.enter_async_context(client)
        bot.start_scheduler(
            application.bot,
            lambda: start_due(api.state.sessions, api.state.worker),
        )
        logger.info(
            f"Bot up for {bot_settings.user_email} on {bot_settings.provider} "
            f"model {bot_settings.model}; payment "
            f"{'ON' if settings.submit_payment else 'OFF (dry runs)'}"
        )

    async def stopped(application: Application) -> None:
        await bot.stop_watching()
        await stack.aclose()
        logger.info("Waiting for runs in progress to finish…")
        await asyncio.to_thread(api.state.worker.shutdown)

    application = (
        Application.builder()
        .token(bot_settings.telegram_token)
        # A tee-time search takes minutes; don't let it hold up the buttons.
        .concurrent_updates(True)
        .post_init(started)
        .post_shutdown(stopped)
        .build()
    )
    bot.install(application)
    return application


def main() -> None:
    # loguru's default handler prints every variable in a traceback, and the
    # Telegram client's frames hold the bot token.
    logger.remove()
    logger.add(sys.stderr, diagnose=False)
    # bootstrap_retries=-1: started while offline, keep trying to reach
    # Telegram rather than exit. Once up, polling retries a lost connection
    # by itself.
    build_application().run_polling(
        allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1
    )
