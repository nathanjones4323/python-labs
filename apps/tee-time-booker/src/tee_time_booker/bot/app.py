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
import tempfile
from contextlib import AsyncExitStack
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import anthropic
from fastmcp import Client
from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
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
from tee_time_booker.bot.tools import BookingTools
from tee_time_booker.bot.voice import Transcriber, VoiceUnavailable
from tee_time_booker.config import BotSettings, Settings
from tee_time_booker.mcp import create_server

# How often a watched run is checked. Runs take minutes, or up to 95 min
# when waiting on a release.
RUN_POLL_S = 30
# Telegram's limit is 4096 characters per message.
MAX_MESSAGE = 4000

HELP = """\
Tell me what you'd like to book, by text or voice note. For example:
• what's open at Saticoy this Saturday morning?
• book my Soule plan for next Saturday, 4 players after 9
• how did my last booking go?

Real payments always wait for you to tap Confirm.
{payment_mode}

/new starts a fresh conversation. /runs shows recent runs."""


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
            "Payment is ON: a confirmed booking really pays."
            if self.pays
            else "Payment is OFF (TEE_TIME_SUBMIT_PAYMENT): every run is a dry run."
        )
        await update.effective_message.reply_text(HELP.format(payment_mode=mode))

    async def _new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.agent.reset(update.effective_chat.id)
        await update.effective_message.reply_text("Starting fresh.")

    async def _runs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        result = await self.tools.call("list_runs", {"limit": 5})
        if result.is_error:
            await update.effective_message.reply_text(result.text[:MAX_MESSAGE])
            return
        runs = result.json()
        lines = [format_run_line(r) for r in runs] or ["No runs yet."]
        await update.effective_message.reply_text("\n".join(lines))

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
        await message.reply_text(f"🎙 {text}")
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
            await query.edit_message_text(
                f"{query.message.text}\n\nCancelled. Nothing was paid."
            )
            await self.agent.note(payment.chat_id, "[The person tapped Cancel.]")
            return
        await query.edit_message_text(
            f"{query.message.text}\n\nConfirmed, starting the run…"
        )
        result, run = await self.agent.confirm(payment)
        if run is None:
            await self._send(
                context, payment.chat_id, f"Couldn't start it: {result.text}"
            )
            return
        await self._started(context, payment.chat_id, run)

    async def _on_error(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        logger.opt(exception=context.error).error("Telegram handler failed")
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                update.effective_chat.id, f"Something went wrong: {context.error}"
            )

    # --- helpers ----------------------------------------------------------

    async def _converse(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
    ) -> None:
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        reply = await self.agent.respond(chat_id, text)
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
                        "Confirm and pay", callback_data=f"pay:{payment.id}"
                    ),
                    InlineKeyboardButton(
                        "Cancel", callback_data=f"cancel:{payment.id}"
                    ),
                ]
            ]
        )
        text = await self._describe_payment(payment)
        await context.bot.send_message(payment.chat_id, text, reply_markup=keyboard)

    async def _describe_payment(self, payment: PendingPayment) -> str:
        """What a Confirm will book, read from the plan or request itself
        rather than from anything the model said."""
        if payment.tool == "book_plan":
            looked_up = await self.tools.call(
                "get_plan", {"plan_id": payment.arguments["plan_id"]}
            )
        else:
            looked_up = await self.tools.call(
                "get_request", {"request_id": payment.arguments["request_id"]}
            )
        if looked_up.is_error:
            return f"💳 Book and pay? ({payment.tool} {payment.arguments})"
        row = looked_up.json()
        target = (
            payment.arguments.get("target_date")
            or row.get("target_date")
            or row.get("next_target_date")
        )
        which = "earliest" if row["prefer_earlier"] else "latest"
        lines = [
            "💳 Book and pay?",
            f"{row['course']}, {format_date(target)}",
            f"Tee-off {format_time(row['earliest'])}–{format_time(row['latest'])}, "
            f"{row['players']} players, {row['holes']} holes",
            f"The run takes the {which} open slot in that window and pays at checkout.",
        ]
        if payment.arguments.get(
            "wait_for_release", True
        ) and not payment.arguments.get("target_date"):
            lines.append(
                "If that day isn't released yet, it waits for the release first."
            )
        return "\n".join(lines)

    async def _started(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, run: StartedRun
    ) -> None:
        kind = "Dry run" if run.dry_run else "Booking"
        await context.bot.send_message(
            chat_id,
            f"{kind} started for {run.course} on {format_date(run.target_date)}. "
            "I'll message you when it finishes.",
        )
        self._watch(context, chat_id, run)

    def _watch(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, run: StartedRun
    ) -> None:
        context.application.create_task(self._follow(context, chat_id, run))

    async def _follow(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, run: StartedRun
    ) -> None:
        while True:
            await asyncio.sleep(RUN_POLL_S)
            result = await self.tools.call("get_run", {"run_id": run.run_id})
            if result.is_error:
                logger.warning(f"Checking run {run.run_id}: {result.text}")
                continue
            body = result.json()
            if body["status"] != "running":
                await self._send(context, chat_id, format_outcome(run, body))
                return

    async def _send(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str
    ) -> None:
        for start in range(0, max(len(text), 1), MAX_MESSAGE):
            await context.bot.send_message(
                chat_id, text[start : start + MAX_MESSAGE] or "…"
            )


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
    where = f"{started.course} on {format_date(started.target_date)}"
    slot = _slot(run)
    fee = f" Green fee ${run['green_fee']} per player." if run.get("green_fee") else ""
    match run["status"]:
        case "success":
            confirmation = (
                f" Confirmation: {run['confirmation']}."
                if run.get("confirmation")
                else ""
            )
            return f"✅ Booked {where} at {slot}.{fee}{confirmation}"
        case "dry_run":
            return (
                f"Dry run done: reached checkout for {slot or 'a slot'} at {where}. "
                f"Nothing was paid.{fee}"
            )
        case "unknown":
            return (
                f"⚠️ Payment was submitted for {where} {slot}, but no confirmation "
                "appeared. Check your account on the booking site before trying again."
            )
        case _:
            return f"❌ The run for {where} failed: {run.get('detail') or 'no detail'}"


def format_run_line(run: dict[str, Any]) -> str:
    started = datetime.fromisoformat(run["started_at"]).astimezone()
    slot = _slot(run)
    return f"{started:%b %d %H:%M}  {run['status']}" + (f"  {slot}" if slot else "")


# --- entry point --------------------------------------------------------------


def build_application() -> Application:
    settings = Settings.from_env()
    bot_settings = BotSettings.from_env()
    api = create_app(settings)
    client = Client(create_server(api))
    tools = BookingTools(client)
    agent = BookingAgent(
        anthropic.AsyncAnthropic(), tools, bot_settings, pays=settings.submit_payment
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
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)
