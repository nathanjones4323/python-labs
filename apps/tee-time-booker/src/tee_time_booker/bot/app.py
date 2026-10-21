"""The Telegram side: who may talk to the bot, buttons, voice, run updates.

    python -m tee_time_booker.bot

Long polling, so the machine opens no ports: the bot asks Telegram for new
messages. Every update from anyone outside `TELEGRAM_ALLOWED_USER_IDS` is
dropped before any handler sees it.

The API, its booking worker and the MCP tools all live in this process (see
`agent.py` for the conversation). A run started from a chat is watched here
and its outcome sent to that chat. The watch is in memory: a run still going
when the bot stops isn't reported after a restart (`/runs` shows it).
"""

import asyncio
import html
import sys
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import anthropic
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
from tee_time_booker.bot.agent import BookingAgent, PendingPayment, StartedRun
from tee_time_booker.bot.rich import Progress, edit_rich, escape, send_rich
from tee_time_booker.bot.tools import BookingTools
from tee_time_booker.bot.voice import Transcriber, VoiceUnavailable
from tee_time_booker.config import BotSettings, Settings
from tee_time_booker.mcp import create_server

# How often a watched run is checked. Runs take minutes, or up to 95 min
# when waiting on a release.
RUN_POLL_S = 30
# Consecutive failed checks before a watch gives up (10 minutes' worth).
MAX_WATCH_ERRORS = 20
# Telegram's limits: 4096 characters per plain message, 32768 per rich one.
MAX_MESSAGE = 4000
MAX_RICH_MESSAGE = 30000
# How long a message to the chat keeps retrying through a dropped connection
# before it's given up on, and the longest wait between tries.
SEND_RETRY_S = 15 * 60
SEND_BACKOFF_MAX_S = 30
# Claude calls retry with backoff on connection errors, timeouts and 5xx.
CLAUDE_MAX_RETRIES = 6

# Rich Markdown: a single newline doesn't break a line, a blank one does.
HELP = """\
**Tell me what you'd like to book**, by text or voice note. For example:

- what's open at Saticoy this Saturday morning?
- book my Soule plan for next Saturday, 4 players after 9
- how did my last booking go?

Real payments always wait for you to tap **Confirm**. {payment_mode}

- /new starts a fresh conversation
- /runs shows recent runs"""

RUN_STATUS = {
    "running": "⏳ running",
    "success": "✅ booked",
    "dry_run": "🧪 dry run",
    "unknown": "⚠️ unconfirmed",
    "error": "❌ failed",
}


class BookingBot:
    def __init__(
        self,
        agent: BookingAgent,
        tools: BookingTools,
        transcriber: Transcriber,
        settings: BotSettings,
        *,
        pays: bool,
    ) -> None:
        self.agent = agent
        self.tools = tools
        self.transcriber = transcriber
        self.settings = settings
        self.pays = pays
        # Plain asyncio tasks, not `Application.create_task`: the Application
        # awaits its own tasks on stop, and a watch can run for 95 minutes.
        self._watches: set[asyncio.Task] = set()

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
        await self._close_prompt(
            context, query, payment, "Confirmed, starting the run…"
        )
        result, run = await self.agent.confirm(payment)
        if run is None:
            await self._send(
                context, payment.chat_id, f"Couldn't start it: {escape(result.text)}"
            )
            return
        await self._started(context, payment.chat_id, run)

    async def _close_prompt(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        query: Any,
        payment: PendingPayment,
        status: str,
    ) -> None:
        """Swap the Confirm / Cancel buttons for what became of them.

        The prompt is drawn again rather than read back: a rich message has
        no `text`, and python-telegram-bot doesn't parse its content yet.
        """
        card = await self._describe_payment(payment)
        try:
            await edit_rich(
                context.bot,
                payment.chat_id,
                query.message.message_id,
                f"{card}\n\n<footer>{status}</footer>",
            )
        except BadRequest:
            logger.exception("Editing the payment prompt")
            await query.edit_message_reply_markup(None)
            await self._send(context, payment.chat_id, status)

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
        await self._send(context, chat_id, reply.text)
        for payment in reply.payments:
            await self._ask_to_pay(context, payment)
        for run in reply.runs:
            self._watch(context, chat_id, run)

    async def _ask_to_pay(
        self, context: ContextTypes.DEFAULT_TYPE, payment: PendingPayment
    ) -> None:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Confirm and pay",
                        callback_data=f"pay:{payment.id}",
                        style="success",
                    ),
                    InlineKeyboardButton(
                        "Cancel", callback_data=f"cancel:{payment.id}"
                    ),
                ]
            ]
        )
        text = await self._describe_payment(payment)
        await send_with_retry(context.bot, payment.chat_id, text, reply_markup=keyboard)

    async def _describe_payment(self, payment: PendingPayment) -> str:
        """What a Confirm will book, read from the plan or request itself
        rather than from anything the model said."""
        fallback = (
            f"**💳 Book and pay?**\n\n{escape(f'{payment.tool} {payment.arguments}')}"
        )
        if payment.tool == "book_plan":
            looked_up = await self.tools.call(
                "get_plan", {"plan_id": payment.arguments.get("plan_id")}
            )
        else:
            looked_up = await self.tools.call(
                "get_request", {"request_id": payment.arguments.get("request_id")}
            )
        if looked_up.is_error:
            return fallback
        try:
            return self._payment_lines(payment, looked_up.json())
        except (KeyError, TypeError, ValueError):
            # Malformed model arguments (e.g. an unparseable target_date) must
            # not cost the person their Confirm / Cancel buttons.
            logger.exception("Describing a held payment")
            return fallback

    @staticmethod
    def _payment_lines(payment: PendingPayment, row: dict[str, Any]) -> str:
        target = (
            payment.arguments.get("target_date")
            or row.get("target_date")
            or row.get("next_target_date")
        )
        which = "earliest" if row["prefer_earlier"] else "latest"
        details = {
            "Course": row["course"],
            "Date": format_date(target),
            "Tee-off": f"{format_time(row['earliest'])} – {format_time(row['latest'])}",
            "Group": f"{row['players']} players · {row['holes']} holes",
        }
        # An HTML table: a Markdown one would need a header row.
        cells = "".join(
            f"<tr><td><b>{label}</b></td><td>{html.escape(str(value))}</td></tr>"
            for label, value in details.items()
        )
        lines = [
            "**💳 Book and pay?**",
            f"<table>{cells}</table>",
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

    async def _started(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, run: StartedRun
    ) -> None:
        kind = "Dry run" if run.dry_run else "Booking"
        await self._send(
            context,
            chat_id,
            f"**{kind} started** · {escape(run.course)}, "
            f"{format_date(run.target_date)}\n\nI'll message you when it finishes.",
        )
        self._watch(context, chat_id, run)

    def _watch(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, run: StartedRun
    ) -> None:
        task = asyncio.create_task(self._follow(context, chat_id, run))
        self._watches.add(task)
        task.add_done_callback(self._watch_done)

    def _watch_done(self, task: asyncio.Task) -> None:
        self._watches.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.opt(exception=task.exception()).error("Watching a run failed")

    async def stop_watching(self) -> None:
        """Drop every watch (the runs themselves carry on; `/runs` shows them)."""
        for task in list(self._watches):
            task.cancel()
        await asyncio.gather(*self._watches, return_exceptions=True)

    async def _follow(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, run: StartedRun
    ) -> None:
        errors = 0
        while True:
            await asyncio.sleep(RUN_POLL_S)
            result = await self.tools.call("get_run", {"run_id": run.run_id})
            if result.is_error:
                logger.warning(f"Checking run {run.run_id}: {result.text}")
                errors += 1
                if errors >= MAX_WATCH_ERRORS:
                    await self._send(
                        context,
                        chat_id,
                        f"I lost track of the run for {escape(run.course)}; /runs "
                        "shows it.",
                    )
                    return
                continue
            errors = 0
            body = result.json()
            if body["status"] != "running":
                await self._send(context, chat_id, format_outcome(run, body))
                return

    async def _send(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str
    ) -> None:
        for chunk in _chunks(text, MAX_RICH_MESSAGE):
            await send_with_retry(context.bot, chat_id, chunk)


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


def _slot(run: dict[str, Any]) -> str:
    if not run.get("tee_time_local"):
        return ""
    start = datetime.fromisoformat(run["tee_time_local"])
    side = f" ({run['side']})" if run.get("side") else ""
    return f"{format_time(start.time().isoformat())}{side}"


def format_outcome(started: StartedRun, run: dict[str, Any]) -> str:
    where = f"{escape(started.course)} on {format_date(started.target_date)}"
    slot = escape(_slot(run))
    fee = (
        f"\n\nGreen fee {escape('$' + str(run['green_fee']))} per player."
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
        anthropic.AsyncAnthropic(max_retries=CLAUDE_MAX_RETRIES),
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
    )
    stack = AsyncExitStack()

    async def started(application: Application) -> None:
        await stack.enter_async_context(client)
        logger.info(
            f"Bot up for {bot_settings.user_email}; payment "
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
