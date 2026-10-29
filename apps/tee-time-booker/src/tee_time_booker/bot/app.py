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
from tee_time_booker.bot.agent import BookingAgent, Reply
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
from tee_time_booker.cards import (
    PendingPayment,
    StartedRun,
    TeeTimeOffer,
    course_name,
    describe_payment,
    format_offer,
    format_outcome,
    format_runs,
    scheduled_status,
    slot_label,
)
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
        return await course_name(self.tools.call, key)

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
        return await describe_payment(self.tools.call, payment, self.pays)

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
